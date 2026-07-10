"""
Gemini brain — the single reasoning + tool-calling model (2-layer architecture).

This is the ONLY orchestrating model. It answers ordinary turns directly and,
when the user wants an action, calls the tools itself: create_task, query_tasks,
update_task, update_task_status, research, update_profile.

Why Gemini (via the OpenAI-compatible endpoint) and not a self-hosted SLM:
  • The previous brain was Groq's llama-3.3-70b. Its documented weakness is
    tool-call formatting — it would sometimes emit a malformed `<function(...)>`
    call as ordinary text, which then got spoken aloud. We carried a whole
    OpenRouter fallback path just to patch that. Gemini has reliable NATIVE
    function calling, so that entire crutch is gone.
  • Groq's free tier caps at ~6K tokens/min, so a long voice conversation (system
    prompt + tools + history + memory ≈ 8K tokens/turn) would 429 after a handful
    of turns and freeze the pipeline. Gemini free tier is 250K TPM with a 1M
    context window — the freeze class of bug disappears.
  • Gemini speaks the OpenAI wire format through
    `https://generativelanguage.googleapis.com/v1beta/openai/`, so we reuse the
    existing AsyncOpenAI client and `run_tool_loop` verbatim — no LangChain, no
    new SDK, no rewrite of the tool loop.

`research` is the one tool backed by a SEPARATE OpenRouter call (GPT-4o-mini with
live web search) so that live-search latency only applies when it actually runs;
everything else is decided and driven here.

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
from services.tools.dispatcher import execute_tool

logger = logging.getLogger(__name__)

# Guard against speaking a raw tool-call/JSON blob aloud if the model ever emits
# one as text instead of a real function call. With Gemini's native function
# calling this is rare (it was a Groq failure mode), but speaking raw JSON over
# TTS is a hard UX failure, so we keep one cheap structural check.
# ponytail: single structural regex, not the old TOOL_REGISTRY-coupled detector.
_STRUCTURAL_LEAK_PATTERN = re.compile(r'[<{]\s*["\']?\w+["\']?\s*[:(]')

# Cap tool rounds so a confused model can't loop forever.
_MAX_TOOL_ROUNDS = 5


def _build_system_prompt() -> str:
    """The single conversational system prompt for the brain.

    Structured per 2026 voice-agent standards: labeled sections, short
    action-oriented rules, voice-first formatting. Built per-call so "today"
    never goes stale on a long-running process — the model needs the real
    current date to resolve recurring events and to know which time-varying
    facts it must verify with research. "Now" is anchored in the user's local
    zone (IST by default) so clock times ("8pm") resolve to the right instant.
    """
    from utils import timez
    now = timez.now_local()
    today = now.strftime("%A, %d %B %Y")
    current_time = f"{now.strftime('%H:%M')} {timez.tz_label()}"
    return (
        "# Role\n"
        "You are a voice assistant that helps the user manage reminders, plans, and "
        "tasks — and researches the real-world facts those depend on. You are heard, "
        "not read: reply in 1-3 short, natural sentences. No markdown, no lists, no "
        "reading out IDs, URLs, or raw data aloud.\n\n"

        f"# Now\n"
        f"Today is {today}; the time is {current_time}. Resolve every relative or "
        "recurring-event date ('this December', 'next month', 'the winter exam') "
        "against this date — never your training data, which is stale. When the user "
        "gives a clock time ('9pm', 'noon', 'in an hour'), resolve it against the "
        "current time and always keep the time-of-day when you record a due date — "
        "never collapse it to midnight. For loose phrases ('Saturday evening') use a "
        "start/end window instead of a single timestamp.\n\n"

        "# Scope & personalization\n"
        "Stay focused on the user's tasks, reminders, planning, and the facts behind "
        "them. Answer clearly-unrelated trivia in one line, or steer back. If you know "
        "the user's location or timezone, use them — research and answer for their "
        "city and resolve times in their zone without making them repeat it. Save a "
        "stable detail (like where they're based) as soon as they mention it.\n\n"

        "# Research\n"
        "Any specific, time-varying fact — a date, deadline, price, schedule, or "
        "registration window — is stale in your training data by definition. Verify it "
        "with the research tool BEFORE answering or creating a task, even when you feel "
        "certain; that certainty is the trap. You do NOT need research for stable "
        "general knowledge (e.g. 'what is the JLPT'). After a research call returns, "
        "NEVER read its findings back verbatim — it's written as a dense reference, not "
        "speech. Paraphrase in your own 1-3 short spoken sentences: pick only the 1-2 "
        "most relevant results (prioritize ones matching the user's known location over "
        "others), state the single most useful fact (the date, the link exists, the "
        "price), and offer to save it as a task or share more instead of listing "
        "everything. Copying the tool's text or its formatting into your reply is "
        "always wrong, even partially.\n\n"

        "# Creating tasks — ask first unless told\n"
        "Before creating a task, judge one thing: did the user EXPLICITLY ask you to "
        "create, track, set, or remind them of something, or are you only inferring it "
        "would help?\n"
        "- Explicitly asked → create it (confirmed), then confirm naturally.\n"
        "- Only inferring (they asked a question, didn't ask for a task) → do NOT "
        "create anything; offer first ('Want me to set a reminder for that?') and wait "
        "for yes.\n"
        "- A plain question ('when is X', 'tell me about X') is never a request to "
        "create. Answer it; at most offer a reminder.\n"
        "- If the user objects to a task you made, acknowledge and offer to cancel it. "
        "Never silently repeat a tool you already ran this turn.\n\n"

        "# Editing vs creating\n"
        "If the user adds or corrects information on something already tracked ('add "
        "the link to that reminder', 'the fee is actually 1500'), UPDATE the existing "
        "task. Never re-create it — that makes a duplicate. Existing tasks related to "
        "what the user just said are surfaced to you in context; check them first.\n\n"

        "# Multi-step plans\n"
        "For a multi-step goal, build a tree: one parent task for the goal, each step "
        "nested under it. When a step can't start until an earlier one finishes, mark "
        "it as depending on that earlier task — it stays blocked until the prerequisite "
        "is done, then unblocks automatically (e.g. sitting an exam depends on "
        "registering first). This applies even when the prerequisite was created in an "
        "earlier turn: check the existing-tasks context and link to it, rather than "
        "creating an unlinked task. Research findings (dates, links) are attached to the "
        "task automatically when you researched earlier this turn — don't re-type them.\n\n"

        "# Answering about tasks\n"
        "Never answer from memory — look the tasks up first and answer from what comes "
        "back. Map the user's wording yourself: a named task → search it; a time range "
        "('next month', 'this week') → a date range you compute from today; 'overdue' / "
        "'finished' → the matching filter.\n\n"

        "# Style\n"
        "Never narrate that you're about to use a tool — just use it. Keep spoken "
        "confirmations short and natural."
    )


async def run_tool_loop(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    session_context: dict,
) -> AsyncGenerator[dict, None]:
    """
    The multi-round tool-calling loop that drives the brain.

    `messages` must already include the system prompt, prior history, and the
    current user turn. It is mutated in place with assistant/tool turns so the
    caller can persist the final exchange.

    Text from a round that also calls tools is buffered and DISCARDED from
    speech; only a tool-less final round is yielded as spoken text — this is
    what stops a stale pre-research answer from being voiced over the real one.
    """
    # Deterministic auto-attach: the most recent successful `research` result
    # THIS turn (across rounds) gets threaded into create_task/update_task args
    # automatically when the model doesn't supply its own research_summary/
    # source_links. Confirmed failure mode otherwise: the model speaks the
    # findings but never copies them into the task-writing call, so they're
    # spoken once and then permanently lost — a data-plumbing bug, not a
    # model-judgment one, so it's fixed in code rather than prompted away.
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
                            tc.index, {"id": None, "name": "", "args": "", "extra": None}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["args"] += tc.function.arguments
                        # Capture provider-specific extras on the tool-call delta —
                        # notably Gemini's `extra_content.google.thought_signature`.
                        # Gemini's thinking models (gemini-flash-latest → 3.5-flash)
                        # REQUIRE this signature echoed back in the assistant message
                        # on the next round, or the follow-up request 400s
                        # ("Function call is missing a thought_signature"). Dropping
                        # it broke every tool-using turn.
                        extra = getattr(tc, "model_extra", None)
                        if extra:
                            slot["extra"] = {**(slot["extra"] or {}), **extra}
        except openai.APIError as exc:
            # No fallback model any more (Gemini's native tool calling is reliable,
            # which is the whole reason we switched off Groq). A hard API failure
            # surfaces as a graceful spoken line rather than dead air or a leak.
            logger.error("Brain API error: %s", exc)
            yield {"type": "text", "text": "Sorry, I hit a snag there — could you say that again?"}
            return

        if not tool_calls:
            # Final answer round. Never speak a raw tool-call/JSON blob aloud.
            if assistant_content and _STRUCTURAL_LEAK_PATTERN.search(assistant_content):
                logger.warning(
                    "Suppressed a leaked-tool-call-looking final answer: %.200s",
                    assistant_content,
                )
                yield {"type": "text", "text": "Sorry, let me try that again."}
                return
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
                    # Echo back Gemini's thought_signature (see capture note above).
                    **(s.get("extra") or {}),
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


class GeminiBrain:
    """The single reasoning + tool-calling model (Gemini), via the OpenAI-compatible
    endpoint. Reuses the shared `run_tool_loop` — this class is just the client +
    system prompt wiring."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.GOOGLE_API_KEY,
            base_url=settings.GEMINI_BASE_URL,
            # Gemini's free tier returns transient 503s under load (observed in
            # practice). Keep the per-attempt timeout short and retries modest —
            # worst case here (~15s x 2 retries) is still bounded by the hard
            # per-turn deadline in main.py's `_produce_text`, which is the real
            # backstop against the pipeline ever reading as "stuck."
            timeout=15.0,
            max_retries=2,     # SDK backs off (honoring Retry-After) on 429/5xx
        )
        self.model = settings.GEMINI_MODEL
        logger.info("Gemini brain initialised  model=%s", self.model)

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
