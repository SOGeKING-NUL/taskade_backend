"""
Google Gemini Flash LLM service — async streaming.
"""

import logging
from typing import AsyncGenerator

import google.generativeai as genai

from config import settings

logger = logging.getLogger(__name__)


class GeminiLLM:
    """Stream chat completions from Google Gemini Flash."""

    def __init__(self) -> None:
        genai.configure(api_key=settings.GOOGLE_API_KEY)
        self.model = genai.GenerativeModel(
            settings.LLM_MODEL,
            system_instruction=settings.LLM_SYSTEM_PROMPT,
        )
        logger.info("Gemini LLM initialised  model=%s", settings.LLM_MODEL)

    # ------------------------------------------------------------------ #
    async def stream_response(
        self,
        user_message: str,
        conversation_history: list | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream LLM tokens for *user_message* (with optional history).

        Parameters
        ----------
        user_message : str
            The latest user utterance (from STT).
        conversation_history : list | None
            Previous turns in ``[{"role": "user"|"model", "parts": ["…"]}]`` format.

        Yields
        ------
        str   Text token strings as they arrive.
        """
        messages: list[dict] = list(conversation_history or [])
        messages.append({"role": "user", "parts": [user_message]})

        logger.info("LLM prompt (%d turns) → %.100s…", len(messages), user_message)

        response = await self.model.generate_content_async(
            messages,
            stream=True,
        )

        async for chunk in response:
            if chunk.text:
                yield chunk.text
