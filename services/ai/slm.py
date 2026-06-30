"""
Groq SLM — the single reasoning + tool-calling brain (2-layer architecture).

This is now the ONLY orchestrating model. It answers ordinary turns directly and,
when the user wants an action, calls the tools itself: create_task, query_tasks,
update_task_status, and research. `research` is the one tool backed by a separate
OpenRouter web-search model (so live-search latency only applies when it actually
runs); everything else is decided and driven here.

Streams provider-agnostic events the pipeline already understands:
    {"type": "text", "text": "..."}                 # speak this (final answer only)
    {"type": "tool.start", "name": "research"}
    {"type": "tool.result", "name": "...", "ok": bool, "summary": "..."}

Correctness rule: text produced in a round that ALSO calls tools is NEVER spoken —
only a round with no tool calls (the final answer) is. This structurally prevents
a stale pre-research answer from being voiced over the real, researched one.
"""

import json
import logging
import re
from typing import AsyncGenerator

from openai import AsyncOpenAI
import openai

from core.config import settings
from services.tools.schemas import get_tool_declarations
from services.tools.dispatcher import execute_tool, TOOL_REGISTRY

logger = logging.getLogger(__name__)

# ── Two-layer leaked-tool-call detector ──────────────────────────────────
# Primary signal: any of our own registered tool names appearing verbatim in
# text that's supposed to be spoken.  Built FROM TOOL_REGISTRY so it can't
# drift out of sync as tools are added/removed.
_TOOL_NAME_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in TOOL_REGISTRY) + r")\b"
)
# Secondary signal: generic structural anomaly — an opening brace/angle
# bracket immediately followed by what looks like a key or function name.
_STRUCTURAL_LEAK_PATTERN = re.compile(r'[<{]\s*["\']?\w+["\']?\s*[:(]')


def _looks_like_leaked_tool_call(text: str) -> bool:
    """Return True if `text` looks like a malformed tool call that leaked
    into the spoken response instead of executing properly."""
    return bool(_TOOL_NAME_PATTERN.search(text) or _STRUCTURAL_LEAK_PATTERN.search(text))

# Cap tool rounds so a confused model can't loop forever.
_MAX_TOOL_ROUNDS = 5


def _build_system_prompt() -> str:
    # Built per-call so "today" never goes stale on a long-running process — the
    # model needs the real current date to resolve recurring events and to know
    # which time-varying facts it must verify with research.
    # Anchor "now" in the user's local zone (IST by default) so the model resolves
    # clock times ("8pm") to the right instant and stamps due_at with +05:30.
    from utils import timez
    now = timez.now_local()
    today = now.strftime("%A, %d %B %Y")
    current_time = f"{now.strftime('%H:%M')} {timez.tz_label()}"
    return (
        "You are a voice assistant whose job is to help the user manage "
        "REMINDERS, PLANS, and TASKS — and to research the real-world facts those "
        "depend on. You speak out loud, so keep replies short and natural: 1-3 "
        "sentences, no markdown, no lists read aloud, and never read out IDs, "
        "URLs, or raw data.\n\n"

        f"Today's date is {today} and the current time is {current_time}. "
        "Resolve every relative or recurring-event date reference ('this December', "
        "'next month', 'the winter exam') against THIS date — never against your "
        "training data, which is stale. When the user gives a CLOCK TIME ('9pm', "
        "'3pm', 'noon', 'tonight', 'this evening', 'in an hour'), resolve it "
        "against the current time and ALWAYS include the time component in "
        "`due_at` — never collapse to midnight/date-only. For looser time "
        "phrases ('Saturday evening') where a single timestamp would overstate "
        "precision, use `window_start` / `window_end` instead.\n\n"

        "SCOPE. Stay focused on the user's tasks, reminders, planning, and the "
        "facts behind them. If asked something clearly unrelated (general trivia, "
        "idle chit-chat), give a brief one-line answer or gently steer back to "
        "what you're here for — don't launch into long explanations.\n\n"

        "PERSONALIZATION. A 'What you know about the user' block may give you their "
        "location and timezone — USE them: research and answer for THEIR city (local "
        "exam centres, deadlines, prices), and resolve times in their timezone, "
        "without making them repeat it. When the user tells you a stable detail like "
        "where they're based ('I'm in Delhi'), immediately save it with "
        "`update_profile` so it persists for next time.\n\n"

        "RESEARCH — this is critical. Any SPECIFIC, time-varying fact — a date, "
        "deadline, price, schedule, registration window, opening/closing time — is "
        "stale in your training data BY DEFINITION and changes from year to year. "
        "You MUST call the `research` tool to verify it BEFORE answering or "
        "creating a task, EVEN WHEN YOU FEEL CERTAIN. Your confidence about a "
        "specific current date is exactly the trap. (You do NOT need research for "
        "general knowledge that doesn't change, e.g. 'what is the JLPT'.)\n\n"

        "CREATING TASKS — ASK FIRST UNLESS TOLD. Before you ever call "
        "`create_task`, make one judgment: did the user EXPLICITLY ask you to "
        "create, track, set up, or remind them of something — in whatever words — "
        "or are you only INFERRING that a task might be helpful?\n"
        "- If they explicitly asked: call `create_task` with `user_confirmed=true`, "
        "then confirm naturally ('Done — I've set a reminder for the registration').\n"
        "- If you're only inferring it (they asked a question, didn't ask for a "
        "task): DO NOT create anything. Ask first — 'Want me to set a reminder for "
        "that?' — and wait for them to say yes. (If you do call `create_task` while "
        "unsure, you MUST pass `user_confirmed=false`; it will not be created and "
        "you'll be reminded to ask.)\n"
        "- A plain question ('when is X', 'what's the deadline', 'tell me about X', "
        "'research X') is NOT a request to create anything. Research if needed, "
        "give the answer, and at most OFFER a reminder.\n"
        "- If the user objects to something you did (e.g. you made a task they "
        "didn't want), acknowledge it and offer to undo it by cancelling that task "
        "with `update_task_status`. Don't silently repeat a tool call, and don't "
        "re-run a tool you already ran this turn.\n"
        "- If the user wants to ADD or CORRECT information on something already "
        "tracked ('add the link to that reminder', 'the fee is actually 1500'), "
        "call `update_task` on the EXISTING task. Never call `create_task` again "
        "for it — that produces a duplicate, not an edit.\n\n"

        "WHEN you do build a multi-step goal, make it a TREE: one parent task for "
        "the goal, each step nested under it via `parent_task` (the parent's "
        "title; it becomes a milestone). When a step can't start until an earlier "
        "one is finished, set the later task's `depends_on_task` to the earlier "
        "one — it stays blocked until that prerequisite is marked done, then "
        "unblocks automatically (e.g. an exam that depends on registering first). "
        "This applies EVEN IF the prerequisite was created in an earlier turn, not "
        "just when creating both at once — check the 'existing tasks that may "
        "relate' context block and your own task tool results for something this "
        "new task should depend on or nest under, and link to its exact title via "
        "`depends_on_task`/`parent_task` rather than creating an unlinked task. "
        "Research findings (date, links) are attached to create_task/update_task "
        "automatically when you called `research` earlier this turn — you don't "
        "need to re-type them, just create/update the task normally.\n\n"

        "ANSWERING QUESTIONS ABOUT TASKS. Never answer from memory — always call "
        "`query_tasks` first and answer from what it returns. Map the user's "
        "wording to the query yourself: a specific task ('the Cairo train "
        "reminder') → `search_text`; a time range ('next month', 'in December', "
        "'this week') → `due_after`/`due_before` as ISO dates you compute from "
        "today; 'what's overdue' / 'what have I finished' → the matching scope. Use "
        "`update_task_status` when the user completes or cancels something.\n\n"

        "Never narrate that you're about to use a tool — just call it. Keep spoken "
        "confirmations short and natural."
    )


async def run_tool_loop(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    session_context: dict,
) -> AsyncGenerator[dict, None]:
    """
    The multi-round tool-calling loop — SHARED between the primary Groq SLM and
    the OpenRouterLLM fallback (used when Groq's tool-call formatting fails).

    This used to be duplicated in `services/ai/llm.py` with a stale, much less
    detailed copy of the system prompt and none of the auto-attach/leak-detection
    logic below — meaning every time Groq glitched (a documented, real reliability
    gap for this model) and the pipeline fell back, the user silently got a
    WORSE-instructed model for that turn: no timezone-aware time resolution, no
    `update_task`-vs-`create_task` guidance, no research auto-attach. Parameterizing
    on `client`/`model` and having BOTH callers delegate here closes that gap
    permanently — there is now only one place this logic can drift.

    `messages` must already include the system prompt, prior history, and the
    current user turn. It is mutated in place with assistant/tool turns so the
    caller can persist the final exchange.

    Text from a round that also calls tools is buffered and DISCARDED from
    speech; only a tool-less final round is yielded as spoken text.
    """
    # Deterministic auto-attach: the most recent successful `research` result
    # THIS turn (across rounds) gets threaded into create_task/update_task args
    # automatically when the model doesn't supply its own research_summary/
    # source_links. Confirmed failure mode otherwise: the model speaks the
    # findings but never copies them into the task-writing call, so they're
    # spoken once and then permanently lost — not a model-judgment problem to
    # prompt away, a data-plumbing one to fix in code.
    last_research: dict | None = None

    for _ in range(_MAX_TOOL_ROUNDS):
        assistant_content = ""
        tool_calls: dict[int, dict] = {}

        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=get_tool_declarations(),
                tool_choice="auto",
                stream=True,
                temperature=0.3,
            )

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    # Buffer only — we can't know if this is the spoken final
                    # answer until the round ends without any tool calls.
                    assistant_content += delta.content

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        slot = tool_calls.setdefault(
                            tc.index, {"id": None, "name": "", "args": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["args"] += tc.function.arguments
        except openai.APIError as exc:
            if "Failed to call a function" in str(exc) or "adjust your prompt" in str(exc) or "tool" in str(exc).lower():
                logger.warning("Tool-calling model failed tool formatting: %s. Yielding fallback event.", exc)
                yield {"type": "fallback", "reason": str(exc)}
                return
            raise

        if not tool_calls:
            # Final answer round — but check for leaked tool calls first.
            if assistant_content and _looks_like_leaked_tool_call(assistant_content):
                logger.warning(
                    "Leaked tool call detected in model text — suppressing and "
                    "escalating to fallback. Raw text: %.300s",
                    assistant_content,
                )
                yield {"type": "fallback", "reason": "malformed_tool_call_text"}
                return
            # Safe to speak now.
            if assistant_content:
                yield {"type": "text", "text": assistant_content}
            messages.append({"role": "assistant", "content": assistant_content})
            return

        # Tool round: any assistant_content here is pre-tool "thinking" —
        # keep it for context but NEVER speak it.
        messages.append({
            "role": "assistant",
            "content": assistant_content or None,
            "tool_calls": [
                {
                    "id": s["id"],
                    "type": "function",
                    "function": {"name": s["name"], "arguments": s["args"] or "{}"},
                }
                for s in tool_calls.values()
            ],
        })

        # Execute each requested tool and feed results back.
        for s in tool_calls.values():
            yield {"type": "tool.start", "name": s["name"]}
            try:
                args = json.loads(s["args"]) if s["args"].strip() else {}
            except json.JSONDecodeError:
                args = {}

            # Auto-attach this turn's research findings if the model didn't
            # supply its own — see note above `last_research` declaration.
            if s["name"] in ("create_task", "update_task") and last_research is not None:
                if not args.get("research_summary"):
                    args["research_summary"] = last_research.get("findings", "")
                if not args.get("source_links"):
                    args["source_links"] = last_research.get("links", [])

            result = await execute_tool(s["name"], args, session_context)

            if s["name"] == "research" and result.get("ok"):
                last_research = result

            yield {
                "type": "tool.result",
                "name": s["name"],
                "ok": bool(result.get("ok", "error" not in result)),
                "summary": result.get("summary", ""),
                # The original call args — main.py needs e.g. `scope` to know
                # WHICH kind of query_tasks call this was (only a targeted
                # specific_task lookup should surface the metadata card).
                "args": args,
                # Pass the full result so main.py can extract presentable
                # metadata (links, findings, task details) without a second
                # lookup — only the metadata helper reads these extra fields.
                **{k: v for k, v in result.items()
                   if k not in ("ok", "summary", "error", "needs_confirmation")},
            }
            messages.append({
                "role": "tool",
                "tool_call_id": s["id"],
                "content": json.dumps(result),
            })
        # Loop: let the model use the tool results.

    logger.warning("Tool-calling loop hit max rounds (%d)", _MAX_TOOL_ROUNDS)
    yield {"type": "text", "text": "Sorry, that took too many steps — let's try again."}


class GroqSLM:
    """The single reasoning + tool-calling model (Groq) — primary path."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.GROQ_API_KEY,
            base_url=settings.GROQ_BASE_URL,
            timeout=30.0,      # fail fast — never let a hung call stall a turn
            max_retries=2,     # SDK backs off (honoring Retry-After) on 429/5xx
        )
        self.model = settings.SLM_MODEL
        logger.info("Groq SLM initialised  model=%s", self.model)

    @property
    def system_prompt(self) -> str:
        return _build_system_prompt()

    async def run_conversation(
        self,
        messages: list[dict],
        session_context: dict,
    ) -> AsyncGenerator[dict, None]:
        async for ev in run_tool_loop(self.client, self.model, messages, session_context):
            yield ev
