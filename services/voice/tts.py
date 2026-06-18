"""
Sarvam AI Text-to-Speech service — WebSocket streaming.

Endpoint : wss://api.sarvam.ai/text-to-speech/ws
Auth     : api-subscription-key header
Query    : model=bulbul:v2&send_completion_event=true
Protocol :
  → {"type": "config", "data": {language, speaker, codec…}}  (first)
  → {"type": "text",   "data": {"text": "…"}}                (per sentence)
  → {"type": "flush"}                                         (after each sentence)
  ← {"type": "audio",  "data": {"audio": "<base64>", …}}     (audio chunks)
  ← {"type": "event",  "data": {"event_type": "final"}}      (done)

Audio codec notes:
  - "wav"  → returns base64-encoded WAV (44-byte header + raw PCM16)
  - "pcm"  → NOT supported (returns 422), despite docs claiming otherwise
  - "mp3"  → returns base64-encoded MPEG audio
  - default (no codec) → returns base64-encoded MPEG audio

We use "wav" and strip the WAV header to get raw PCM16, which the
client can play directly via Web Audio API without any decode overhead.
"""

import asyncio
import base64
import json
import logging
from typing import AsyncGenerator
from urllib.parse import urlencode

import websockets

from core.config import settings

logger = logging.getLogger(__name__)

# Standard WAV header is 44 bytes. Some encoders use extended headers.
_WAV_HEADER_SIZE = 44


def _strip_wav_header(wav_bytes: bytes) -> bytes:
    """
    Strip the WAV/RIFF header from audio data to extract raw PCM samples.

    Handles both standard 44-byte headers and extended headers by looking
    for the "data" chunk marker.
    """
    # Try to find the "data" chunk for robustness
    data_marker = wav_bytes.find(b"data")
    if data_marker >= 0 and data_marker + 8 <= len(wav_bytes):
        # Skip "data" (4 bytes) + chunk size (4 bytes) = 8 bytes after marker
        pcm_start = data_marker + 8
        return wav_bytes[pcm_start:]

    # Fall back to fixed 44-byte offset
    if len(wav_bytes) > _WAV_HEADER_SIZE:
        return wav_bytes[_WAV_HEADER_SIZE:]

    return wav_bytes


class SarvamTTS:
    """Stream text → speech via the Sarvam Bulbul WebSocket API."""

    WS_BASE = "wss://api.sarvam.ai/text-to-speech/ws"

    def __init__(self) -> None:
        self.api_key = settings.SARVAM_API_KEY
        self.voice = settings.SARVAM_TTS_VOICE
        self.language = settings.SARVAM_TTS_LANGUAGE
        self.model = settings.SARVAM_TTS_MODEL
        self.sample_rate = settings.SARVAM_TTS_SAMPLE_RATE
        self.ws: websockets.client.ClientConnection | None = None
        self.lock = asyncio.Lock()

    def _is_connected(self) -> bool:
        if self.ws is None:
            return False
        if hasattr(self.ws, "state"):
            return self.ws.state.name == "OPEN"
        return not getattr(self.ws, "closed", True)

    async def connect(self) -> None:
        """Establish a persistent WebSocket connection to Sarvam."""
        if self._is_connected():
            return

        async with self.lock:
            # Double-check inside lock
            if self._is_connected():
                return

            headers = {"api-subscription-key": self.api_key}
            url = self._build_url()
            self.ws = await websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            )

            # Send configuration (must be first message)
            config = {
                "type": "config",
                "data": {
                    "target_language_code": self.language,
                    "speaker": self.voice,
                    "output_audio_codec": "linear16",
                },
            }
            await self.ws.send(json.dumps(config))
            logger.info(
                "TTS WS persistently connected  voice=%s  model=%s  rate=%d",
                self.voice, self.model, self.sample_rate,
            )

    async def close(self) -> None:
        """Cleanly close the persistent WebSocket."""
        async with self.lock:
            if self.ws is not None:
                if hasattr(self.ws, "state"):
                    if self.ws.state.name != "CLOSED":
                        await self.ws.close()
                else:
                    if not getattr(self.ws, "closed", True):
                        await self.ws.close()
                self.ws = None
                logger.info("TTS WS closed cleanly")

    def _build_url(self) -> str:
        """Build the WebSocket URL with model and completion event as query params."""
        params = urlencode({
            "model": self.model,
            "send_completion_event": "true",
        })
        return f"{self.WS_BASE}?{params}"

    # ------------------------------------------------------------------ #
    async def stream_tts(
        self, text_chunks: AsyncGenerator[str, None]
    ) -> AsyncGenerator[bytes, None]:
        """
        Feed sentences from *text_chunks* into the persistent TTS WebSocket
        and yield raw PCM-16 audio as it arrives.

        Audio arrives as base64-encoded raw PCM16 chunks inside JSON messages.
        We decode base64 and yield raw PCM16 bytes directly.

        Parameters
        ----------
        text_chunks : AsyncGenerator[str, None]
            An async generator that yields sentence-level strings.

        Yields
        ------
        bytes   Raw PCM-16 audio data (no WAV header).
        """
        await self.connect()

        ws = self.ws
        if not ws:
            raise RuntimeError("TTS WebSocket is not connected.")

        text_done = asyncio.Event()

        async def _send_text() -> None:
            try:
                async for chunk in text_chunks:
                    chunk = chunk.strip()
                    if not chunk:
                        continue

                    text_msg = {
                        "type": "text",
                        "data": {"text": chunk},
                    }
                    await ws.send(json.dumps(text_msg))
                    logger.debug("TTS text sent: %.60s", chunk)

                    await ws.send(json.dumps({"type": "flush"}))
                    logger.debug("TTS flush sent")

            except Exception as exc:  # noqa: BLE001
                logger.error("TTS send error: %s", exc)
            finally:
                text_done.set()

        send_task = asyncio.create_task(_send_text())

        try:
            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=15.0)
                except asyncio.TimeoutError:
                    if text_done.is_set():
                        try:
                            message = await asyncio.wait_for(
                                ws.recv(), timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            logger.warning("TTS timed out after text done")
                            break
                    else:
                        continue

                if isinstance(message, bytes):
                    yield message
                else:
                    data = json.loads(message)
                    msg_type = data.get("type", "")

                    if msg_type == "audio":
                        audio_data = data.get("data", {})
                        b64_audio = ""
                        if isinstance(audio_data, dict):
                            b64_audio = audio_data.get("audio", "")
                        elif isinstance(audio_data, str):
                            b64_audio = audio_data

                        if b64_audio:
                            pcm_bytes = base64.b64decode(b64_audio)
                            if pcm_bytes:
                                yield pcm_bytes
                        else:
                            logger.warning("TTS audio message with no data")

                    elif msg_type == "event":
                        event_data = data.get("data", {})
                        event_type = event_data.get("event_type", "")
                        if event_type == "final":
                            logger.info("TTS synthesis complete (final event)")
                            break
                        else:
                            logger.debug("TTS event: %s", event_type)

                    elif msg_type == "completion":
                        logger.info("TTS synthesis complete (completion)")
                        break

                    elif msg_type == "error":
                        err_data = data.get("data", {})
                        err_msg = err_data.get("message", str(data))
                        logger.error("TTS error: %s", err_msg)
                        break

                    else:
                        logger.debug("TTS msg type=%s", msg_type)

        except websockets.exceptions.ConnectionClosed as exc:
            logger.warning("TTS WS closed: %s", exc)
        except Exception as exc:
            logger.error("TTS connection error: %s", exc)
            raise
        finally:
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass

