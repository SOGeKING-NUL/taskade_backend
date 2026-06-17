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

from openai import AsyncOpenAI, APIError

from config import settings
from services.tools.schemas import ESCALATE_TOOL

logger = logging.getLogger(__name__)

_SLM_SYSTEM_PROMPT = (
    settings.LLM_SYSTEM_PROMPT
    + "\n\nYou are the fast first responder in a voice assistant. Answer the user "
    "directly and conversationally for chat, greetings, and general/factual "
    "questions — even if your knowledge may be a little out of date, just answer. "
    "ONLY call the escalate_to_assistant tool when the user clearly wants to "
    "CREATE, UPDATE, COMPLETE, or LIST/LOOK UP a task or reminder (e.g. 'remind "
    "me to…', 'add a task…', 'what's on my list', 'mark X done'). When you "
    "escalate, call the tool and say nothing else — never narrate that you are "
    "escalating."
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

        # Accumulate a possible escalate tool-call across streamed deltas.
        tool_args = ""
        saw_tool = False

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=[ESCALATE_TOOL],
                tool_choice="auto",
                stream=True,
                temperature=0.3,
            )

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

        except APIError as exc:
            # Groq's small model occasionally fails to emit a well-formed tool
            # call ("Failed to call a function"). That failure itself means the
            # model wanted to act — so escalate by fallback rather than crash.
            detail = str(exc).lower()
            if "function" in detail or "tool" in detail:
                logger.warning("SLM tool-call malformed — escalating by fallback (%s)", exc)
                yield {"type": "escalate", "intent_summary": user_message, "category": "other"}
                return
            raise

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
