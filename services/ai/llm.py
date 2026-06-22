"""
OpenRouter LLM — tool-calling / research path.

Used only when the fast SLM escalates. Runs a full tool-calling loop: stream a
turn, and if the model requests tool calls, execute them via the dispatcher,
feed results back, and continue until the model produces a final spoken answer.

Streams provider-agnostic events:
    {"type": "text", "text": "..."}                 # speak this
    {"type": "tool.start", "name": "create_task"}
    {"type": "tool.result", "name": "...", "ok": bool, "summary": "..."}

OpenRouter is OpenAI-compatible, so this uses the `openai` SDK pointed at the
OpenRouter base URL. (No Gemini — the user has no Gemini credits.)
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


def _build_system_prompt() -> str:
    # Computed per-call (not at import time) so the date never goes stale on a
    # long-running process — the model needs "today" to resolve recurring
    # events ("the December JLPT") and to know whether to escalate a yes/no
    # confirmation from the prior turn.
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    return (
        settings.LLM_SYSTEM_PROMPT
        + f"\n\nToday's date is {today}. Resolve relative or recurring-event "
        "dates ('this December', 'next month') against THIS date, not your "
        "training data, which is stale and biases you toward a past occurrence."
        + "\n\nYou are the action-taking part of a voice assistant. Use the "
        "provided tools to create, update, and look up the user's tasks and "
        "reminders.\n"
        "- Only call `create_task` when the user explicitly asks you to "
        "create/add/set up a task or reminder, OR when you just asked if "
        "they'd like one and they confirmed (e.g. 'yes', 'please', 'go "
        "ahead'). If they decline or didn't ask for one, don't create "
        "anything — answering their question is enough.\n"
        "- If the user is only asking a factual question ('when is X', "
        "'what's the deadline for Y') and hasn't asked you to remember or "
        "track it, do NOT create a task. Call `research` if you need current "
        "facts, answer conversationally, then ask if they'd like a reminder "
        "set up for it — and wait for their answer before calling "
        "create_task.\n"
        "- When a task depends on real-world facts or dates you don't already "
        "know (exam dates, deadlines, prices, schedules), call `research` "
        "first to find them — regardless of whether a task ends up getting "
        "created — and if one does, pass the findings into create_task as "
        "`research_summary`/`source_links` and set `due_at` if you learned a "
        "concrete date.\n"
        "- When an event has a real-world prerequisite (most formal exams or "
        "applications require registering by an earlier deadline before the "
        "event itself), create TWO tasks: one for the prerequisite step and "
        "one for the event, with the event's `depends_on_task` set to the "
        "prerequisite so it stays blocked until that's done. Mention this "
        "plan briefly when confirming.\n"
        "- After running tools, give a short, natural spoken confirmation of "
        "what you did (1-2 sentences). Don't read out IDs, URLs, or JSON."
    )

# Cap tool-call iterations to avoid runaway loops.
_MAX_TOOL_ROUNDS = 5


class OpenRouterLLM:
    """Tool-calling LLM over OpenRouter."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
            timeout=60.0,      # tool loop can be longer, but still bounded
            max_retries=2,     # SDK backs off (honoring Retry-After) on 429/5xx
        )
        self.model = settings.OPENROUTER_LLM_MODEL
        logger.info("OpenRouter LLM initialised  model=%s", self.model)

    @property
    def system_prompt(self) -> str:
        return _build_system_prompt()

    async def run_conversation(
        self,
        messages: list[dict],
        session_context: dict,
    ) -> AsyncGenerator[dict, None]:
        """
        Run the multi-step tool-calling loop.

        `messages` must already include the system prompt, prior history, and the
        current user turn. It is mutated in place with assistant/tool turns so the
        caller can persist the final exchange if desired.
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
                    assistant_content += delta.content
                    yield {"type": "text", "text": delta.content}

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
                # Final answer — done.
                messages.append({"role": "assistant", "content": assistant_content})
                return

            # Record the assistant's tool-call turn.
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

        logger.warning("Tool-calling loop hit max rounds (%d)", _MAX_TOOL_ROUNDS)
        yield {"type": "text", "text": " Sorry, that took too many steps — let's try again."}
