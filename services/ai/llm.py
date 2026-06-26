"""
OpenRouter LLM — fallback tool-calling path.

Used only when the primary Groq SLM's tool-call formatting fails (a documented
Groq/Llama-3.3 reliability gap — see `services/ai/slm.py`). Delegates to the
EXACT same system prompt and tool-calling loop as the primary path
(`slm.run_tool_loop`), just pointed at a different provider/model — so a Groq
hiccup degrades reliability, not behavior. This file used to carry its own,
separately-maintained prompt and loop, which silently drifted out of sync with
every improvement made to the primary path; delegating removes that risk
structurally rather than relying on remembering to update both.

OpenRouter is OpenAI-compatible, so this uses the `openai` SDK pointed at the
OpenRouter base URL.
"""

import logging
from typing import AsyncGenerator

from openai import AsyncOpenAI

from core.config import settings
from services.ai.slm import _build_system_prompt, run_tool_loop

logger = logging.getLogger(__name__)


class OpenRouterLLM:
    """Tool-calling LLM over OpenRouter — fallback path only."""

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
        async for ev in run_tool_loop(self.client, self.model, messages, session_context):
            yield ev
