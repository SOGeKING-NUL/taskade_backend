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
from datetime import datetime, timezone
from typing import AsyncGenerator

from openai import AsyncOpenAI

from core.config import settings
from services.tools.schemas import get_tool_declarations
from services.tools.dispatcher import execute_tool

logger = logging.getLogger(__name__)

# Cap tool rounds so a confused model can't loop forever.
_MAX_TOOL_ROUNDS = 5


def _build_system_prompt() -> str:
    # Built per-call so "today" never goes stale on a long-running process — the
    # model needs the real current date to resolve recurring events and to know
    # which time-varying facts it must verify with research.
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    return (
        "You are a voice assistant whose job is to help the user manage "
        "REMINDERS, PLANS, and TASKS — and to research the real-world facts those "
        "depend on. You speak out loud, so keep replies short and natural: 1-3 "
        "sentences, no markdown, no lists read aloud, and never read out IDs, "
        "URLs, or raw data.\n\n"

        f"Today's date is {today}. Resolve every relative or recurring-event date "
        "reference ('this December', 'next month', 'the winter exam') against THIS "
        "date — never against your training data, which is stale.\n\n"

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
        "re-run a tool you already ran this turn.\n\n"

        "WHEN you do build a multi-step goal, make it a TREE: one parent task for "
        "the goal, each step nested under it via `parent_task` (the parent's "
        "title; it becomes a milestone). When a step can't start until an earlier "
        "one is finished, set the later task's `depends_on_task` to the earlier "
        "one — it stays blocked until that prerequisite is marked done, then "
        "unblocks automatically (e.g. an exam that depends on registering first). "
        "When research gave you a concrete date, pass it as `due_at` (ISO 8601) and "
        "the findings as `research_summary` / `source_links`.\n\n"

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


class GroqSLM:
    """The single reasoning + tool-calling model (Groq)."""

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
        """
        Run the multi-round tool-calling loop.

        `messages` must already include the system prompt, prior history, and the
        current user turn. It is mutated in place with assistant/tool turns so the
        caller can persist the final exchange.

        Text from a round that also calls tools is buffered and DISCARDED from
        speech; only a tool-less final round is yielded as spoken text.
        """
        for _ in range(_MAX_TOOL_ROUNDS):
            assistant_content = ""
            tool_calls: dict[int, dict] = {}

            stream = await self.client.chat.completions.create(
                model=self.model,
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

            if not tool_calls:
                # Final answer round — safe to speak now.
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
                result = await execute_tool(s["name"], args, session_context)
                yield {
                    "type": "tool.result",
                    "name": s["name"],
                    "ok": bool(result.get("ok", "error" not in result)),
                    "summary": result.get("summary", ""),
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": s["id"],
                    "content": json.dumps(result),
                })
            # Loop: let the model use the tool results.

        logger.warning("SLM tool-calling loop hit max rounds (%d)", _MAX_TOOL_ROUNDS)
        yield {"type": "text", "text": "Sorry, that took too many steps — let's try again."}
