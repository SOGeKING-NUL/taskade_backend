"""
Deepgram Streaming STT — WebSocket-based real-time transcription.

Uses Deepgram Nova-3 for:
  - Continuous streaming transcription with interim results
  - Server-side endpointing (silence-based end-of-turn detection)
  - utterance_end events for reliable pipeline triggering

Resilience: the socket can drop for reasons outside our control (Deepgram's
~10s audio-idle timeout during a long TTS playback, a transient network blip,
provider-side load shedding). When it does, the receive loop **auto-reconnects**
instead of dying silently — otherwise one drop leaves the backend permanently
"deaf" with no error surfaced. A periodic KeepAlive keeps the socket open across
silent gaps (while the assistant is speaking and no mic audio is flowing).
"""

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

import websockets

from core.config import settings

logger = logging.getLogger("services.stt")


class DeepgramStreamingSTT:
    """Stream audio → text via the Deepgram Nova-3 WebSocket API."""

    WS_URL = "wss://api.deepgram.com/v1/listen"

    # Send a KeepAlive if no audio has flowed for this long, so Deepgram's
    # ~10s audio-idle timer never closes the socket mid-conversation.
    _KEEPALIVE_SECONDS = 7
    # Reconnect attempts after an unexpected drop, with growing backoff.
    _RECONNECT_ATTEMPTS = 5

    def __init__(self) -> None:
        self.api_key = settings.DEEPGRAM_API_KEY
        self.ws: Optional[websockets.ClientConnection] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._accumulated_transcript: str = ""
        self._on_interim: Optional[Callable] = None
        self._on_final: Optional[Callable] = None
        self._on_utterance_end: Optional[Callable] = None
        self._on_reconnect: Optional[Callable] = None
        self._closing: bool = False          # deliberate close vs. an unexpected drop
        self._last_audio_ts: float = 0.0     # monotonic ts of the last audio frame sent

    # ------------------------------------------------------------------ #
    #  Connection lifecycle
    # ------------------------------------------------------------------ #

    def _build_url(self) -> str:
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
        })
        return f"{self.WS_URL}?{params}"

    async def _open_socket(self) -> None:
        """Open a fresh Deepgram WebSocket (no task wiring — just the socket)."""
        headers = {"Authorization": f"Token {self.api_key}"}
        self.ws = await websockets.connect(
            self._build_url(),
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        )

    async def connect(
        self,
        *,
        on_interim: Optional[Callable[[str], Awaitable[None]]] = None,
        on_final: Optional[Callable[[str], Awaitable[None]]] = None,
        on_utterance_end: Optional[Callable[[str], Awaitable[None]]] = None,
        on_reconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """Open a persistent streaming connection to Deepgram."""
        # Close any existing connection first
        if self.ws is not None:
            await self.close()

        self._closing = False
        self._on_interim = on_interim
        self._on_final = on_final
        self._on_utterance_end = on_utterance_end
        self._on_reconnect = on_reconnect
        self._accumulated_transcript = ""
        self._last_audio_ts = time.monotonic()

        await self._open_socket()

        # Start the background receiver + keepalive
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info(
            "Deepgram connected  model=%s  lang=%s  endpointing=%dms  utterance_end=%dms",
            settings.DEEPGRAM_MODEL,
            settings.DEEPGRAM_LANGUAGE,
            settings.DEEPGRAM_ENDPOINTING_MS,
            settings.DEEPGRAM_UTTERANCE_END_MS,
        )

    async def close(self) -> None:
        """Close the Deepgram WebSocket and stop the background tasks."""
        self._closing = True  # tell the receive loop NOT to reconnect

        for task in (self._receive_task, self._keepalive_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._receive_task = None
        self._keepalive_task = None

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
                self._last_audio_ts = time.monotonic()
            except Exception:
                pass  # silently drop if connection is closing/reconnecting

    # ------------------------------------------------------------------ #
    #  KeepAlive — prevent Deepgram's audio-idle timeout from closing us
    # ------------------------------------------------------------------ #

    async def _keepalive_loop(self) -> None:
        """Send {"type":"KeepAlive"} during silent gaps so the socket survives."""
        try:
            while not self._closing:
                await asyncio.sleep(self._KEEPALIVE_SECONDS)
                if self.ws is None:
                    continue
                idle = time.monotonic() - self._last_audio_ts
                if idle >= self._KEEPALIVE_SECONDS:
                    try:
                        await self.ws.send(json.dumps({"type": "KeepAlive"}))
                    except Exception:
                        pass  # the receive loop handles/repairs a dead socket
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    #  Background receiver (with auto-reconnect)
    # ------------------------------------------------------------------ #

    async def _reopen(self) -> bool:
        """Re-establish the socket after an unexpected drop. Returns success."""
        for attempt in range(1, self._RECONNECT_ATTEMPTS + 1):
            if self._closing:
                return False
            try:
                await self._open_socket()
                self._last_audio_ts = time.monotonic()
                logger.info("Deepgram reconnected (attempt %d)", attempt)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Deepgram reconnect attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(min(attempt, 5))  # 1s,2s,3s,4s,5s backoff
        return False

    async def _handle_message(self, msg: str) -> None:
        data = json.loads(msg)
        msg_type = data.get("type", "")

        if msg_type == "Results":
            channel = data.get("channel", {})
            alternatives = channel.get("alternatives", [])
            transcript = alternatives[0].get("transcript", "") if alternatives else ""
            is_final = data.get("is_final", False)

            if transcript:
                if is_final:
                    # Finalized segment — accumulate
                    self._accumulated_transcript += transcript + " "
                    if self._on_final:
                        await self._on_final(self._accumulated_transcript.strip())
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

    async def _receive_loop(self) -> None:
        """Read Deepgram responses; auto-reconnect if the socket drops unexpectedly."""
        while not self._closing:
            try:
                async for msg in self.ws:
                    await self._handle_message(msg)
                # `async for` ended → the socket closed.
            except asyncio.CancelledError:
                raise  # deliberate shutdown via close()
            except websockets.exceptions.ConnectionClosed:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.error("Deepgram receive error: %s", exc)

            if self._closing:
                break

            # Unexpected drop — try to recover instead of going silently deaf.
            logger.warning("Deepgram socket dropped — attempting reconnect…")
            if self._on_reconnect:
                try:
                    await self._on_reconnect()
                except Exception:  # noqa: BLE001
                    pass
            if not await self._reopen():
                logger.error("Deepgram reconnect failed — STT is down for this session")
                break
            # Loop continues and re-reads from the freshly reopened self.ws.
