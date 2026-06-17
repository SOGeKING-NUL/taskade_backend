"""
Groq SLM — fast conversational path.

Answers ordinary turns directly (low latency, Groq direct). When the user wants
an ACTION (task/reminder/research/lookup) it calls the `escalate_to_assistant`
tool instead of answering, and the pipeline hands off to the OpenRouter LLM.

Streams provider-agnostic events:
    {"type": "text", "text": "..."}                      # speak this
    {"type": "escalate", "intent_summary": "...", "category": "..."}
"""

import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from config import settings
from services.tools.schemas import ESCALATE_TOOL

logger = logging.getLogger(__name__)

_SLM_SYSTEM_PROMPT = (
    settings.LLM_SYSTEM_PROMPT
    + "\n\nYou are the fast first responder in a voice assistant. "
    "If the user just wants to chat or asks a general question you can answer "
    "in a sentence or two, answer directly and conversationally. "
    "If the user wants to create/update/complete/look up a task or reminder, or "
    "needs anything researched, do NOT answer — call the escalate_to_assistant "
    "tool and say nothing else. Never describe that you are escalating."
)


class GroqSLM:
    """Fast SLM with a single escalation tool."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=settings.GROQ_API_KEY,
            base_url=settings.GROQ_BASE_URL,
        )
        self.model = settings.SLM_MODEL
        logger.info("Groq SLM initialised  model=%s", self.model)

    async def stream_response(
        self,
        user_message: str,
        conversation_history: list[dict] | None = None,
    ) -> AsyncGenerator[dict, None]:
        messages = [{"role": "system", "content": _SLM_SYSTEM_PROMPT}]
        messages += list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=[ESCALATE_TOOL],
            tool_choice="auto",
            stream=True,
            temperature=0.5,
        )

        # Accumulate a possible escalate tool-call across streamed deltas.
        tool_args = ""
        saw_tool = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.tool_calls:
                saw_tool = True
                for tc in delta.tool_calls:
                    if tc.function and tc.function.arguments:
                        tool_args += tc.function.arguments
                continue

            if delta.content:
                yield {"type": "text", "text": delta.content}

        if saw_tool:
            import json

            try:
                parsed = json.loads(tool_args) if tool_args.strip() else {}
            except json.JSONDecodeError:
                parsed = {}
            logger.info("SLM escalating → %s", parsed.get("intent_summary", "(no summary)"))
            yield {
                "type": "escalate",
                "intent_summary": parsed.get("intent_summary", user_message),
                "category": parsed.get("category", "other"),
            }
