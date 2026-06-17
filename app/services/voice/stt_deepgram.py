"""
Deepgram Streaming STT — WebSocket-based real-time transcription.

Uses Deepgram Nova-3 for:
  - Continuous streaming transcription with interim results
  - Server-side endpointing (silence-based end-of-turn detection)
  - utterance_end events for reliable pipeline triggering

This replaces the previous Sarvam STT batch approach.  Now the client
streams raw PCM continuously and Deepgram decides when the speaker
has finished — eliminating the client-side redemptionMs tradeoff.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

import websockets

from config import settings

logger = logging.getLogger("services.stt")


class DeepgramStreamingSTT:
    """Stream audio → text via the Deepgram Nova-3 WebSocket API."""

    WS_URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self) -> None:
        self.api_key = settings.DEEPGRAM_API_KEY
        self.ws: Optional[websockets.ClientConnection] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._accumulated_transcript: str = ""
        self._on_interim: Optional[Callable] = None
        self._on_final: Optional[Callable] = None
        self._on_utterance_end: Optional[Callable] = None

    # ------------------------------------------------------------------ #
    #  Connection lifecycle
    # ------------------------------------------------------------------ #

    async def connect(
        self,
        *,
        on_interim: Optional[Callable[[str], Awaitable[None]]] = None,
        on_final: Optional[Callable[[str], Awaitable[None]]] = None,
        on_utterance_end: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        """Open a persistent streaming connection to Deepgram."""
        # Close any existing connection first
        if self.ws is not None:
            await self.close()

        params = urlencode({
            "model": settings.DEEPGRAM_MODEL,
            "language": settings.DEEPGRAM_LANGUAGE,
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
            "endpointing": str(settings.DEEPGRAM_ENDPOINTING_MS),
            "utterance_end_ms": str(settings.DEEPGRAM_UTTERANCE_END_MS),
            "interim_results": "true",
            "vad_events": "true",
            "smart_format": "true",
            "punctuate": "true",
            "keepalive": "true",
        })
        url = f"{self.WS_URL}?{params}"
        headers = {"Authorization": f"Token {self.api_key}"}

        self.ws = await websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        )
        self._on_interim = on_interim
        self._on_final = on_final
        self._on_utterance_end = on_utterance_end
        self._accumulated_transcript = ""

        # Start the background receiver
        self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info(
            "Deepgram connected  model=%s  lang=%s  endpointing=%dms  utterance_end=%dms",
            settings.DEEPGRAM_MODEL,
            settings.DEEPGRAM_LANGUAGE,
            settings.DEEPGRAM_ENDPOINTING_MS,
            settings.DEEPGRAM_UTTERANCE_END_MS,
        )

    async def close(self) -> None:
        """Close the Deepgram WebSocket and stop the receive loop."""
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._receive_task = None

        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        self._accumulated_transcript = ""
        logger.info("Deepgram STT closed")

    def reset_transcript(self) -> None:
        """Clear accumulated transcript (e.g. on new utterance start)."""
        self._accumulated_transcript = ""

    # ------------------------------------------------------------------ #
    #  Audio streaming
    # ------------------------------------------------------------------ #

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Forward a raw PCM-16 audio chunk to Deepgram."""
        if self.ws is not None:
            try:
                await self.ws.send(pcm_chunk)
            except Exception:
                pass  # silently drop if connection is closing

    # ------------------------------------------------------------------ #
    #  Background receiver
    # ------------------------------------------------------------------ #

    async def _receive_loop(self) -> None:
        """Background task: read Deepgram JSON responses and fire callbacks."""
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                msg_type = data.get("type", "")

                if msg_type == "Results":
                    channel = data.get("channel", {})
                    alternatives = channel.get("alternatives", [])
                    transcript = (
                        alternatives[0].get("transcript", "")
                        if alternatives
                        else ""
                    )
                    is_final = data.get("is_final", False)

                    if transcript:
                        if is_final:
                            # Finalized segment — accumulate
                            self._accumulated_transcript += transcript + " "
                            if self._on_final:
                                await self._on_final(
                                    self._accumulated_transcript.strip()
                                )
                        else:
                            # Interim (partial) result — show in UI
                            interim = self._accumulated_transcript + transcript
                            if self._on_interim:
                                await self._on_interim(interim.strip())

                elif msg_type == "UtteranceEnd":
                    # Deepgram's endpointing detected end-of-turn
                    transcript = self._accumulated_transcript.strip()
                    if transcript:
                        logger.info("STT utterance_end → %s", transcript)
                        if self._on_utterance_end:
                            await self._on_utterance_end(transcript)
                        self._accumulated_transcript = ""

        except websockets.exceptions.ConnectionClosed:
            logger.warning("Deepgram WebSocket closed")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Deepgram receive error: %s", exc)
