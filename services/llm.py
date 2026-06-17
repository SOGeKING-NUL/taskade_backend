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
from typing import AsyncGenerator

from openai import AsyncOpenAI

from config import settings
from services.tools.schemas import get_tool_declarations
from services.tools.dispatcher import execute_tool

logger = logging.getLogger(__name__)

_LLM_SYSTEM_PROMPT = (
    settings.LLM_SYSTEM_PROMPT
    + "\n\nYou are the action-taking part of a voice assistant. Use the provided "
    "tools to create, update, and look up the user's tasks and reminders. After "
    "running tools, give a short, natural spoken confirmation of what you did "
    "(1-2 sentences). Don't read out IDs or JSON."
)

# Cap tool-call iterations to avoid runaway loops.
_MAX_TOOL_ROUNDS = 5


class OpenRouterLLM:
    """Tool-calling LLM over OpenRouter."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
        )
        self.model = settings.OPENROUTER_LLM_MODEL
        self.system_prompt = _LLM_SYSTEM_PROMPT
        logger.info("OpenRouter LLM initialised  model=%s", self.model)

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
