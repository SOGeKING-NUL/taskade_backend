"""
TTS Speech Engine — FastAPI server.

Single WebSocket endpoint ``/ws/voice`` orchestrates:
    Client audio (continuous stream) → Deepgram STT (server-side endpointing) → LLM (streaming) → Sarvam TTS (streaming) → Client audio

Architecture:
    - Client-side Silero VAD handles instant barge-in detection (~10ms)
    - Deepgram Nova-3 handles end-of-turn detection via server-side endpointing
    - Sarvam Bulbul handles text-to-speech synthesis
"""

import asyncio
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from services.stt_deepgram import DeepgramStreamingSTT
from services.tts import SarvamTTS
from services.llm import GeminiLLM

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("engine")

# ── App & middleware ─────────────────────────────────────────────────────
app = FastAPI(title="TTS Speech Engine", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global services (stateless, shared across connections) ───────────────
llm_service = GeminiLLM()

# ── Sentence-boundary characters ────────────────────────────────────────
_SENTENCE_ENDERS = frozenset(".!?।;:")


# ═════════════════════════════════════════════════════════════════════════
#  Helper
# ═════════════════════════════════════════════════════════════════════════
async def _send_json(ws: WebSocket, data: dict) -> None:
    """Send a JSON text frame — silently ignores closed connections."""
    try:
        await ws.send_json(data)
    except Exception:  # noqa: BLE001
        pass


# ═════════════════════════════════════════════════════════════════════════
#  Voice pipeline (LLM → TTS only — STT is handled by Deepgram callbacks)
# ═════════════════════════════════════════════════════════════════════════
async def run_voice_pipeline(
    ws: WebSocket,
    transcript: str,
    conversation_history: list[dict],
    tts_service: SarvamTTS,
) -> None:
    """
    LLM → TTS cascade for one user turn.

    Receives a pre-computed transcript (from Deepgram utterance_end) and runs it
    through the LLM and TTS pipeline concurrently.
    """

    # ── LLM + TTS (concurrent) ───────────────────────────────────────────
    await _send_json(ws, {"type": "processing", "stage": "llm"})

    sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
    full_response_parts: list[str] = []

    # ── Task A: stream LLM → buffer sentences → push to queue ───────────
    async def _llm_to_sentences() -> None:
        sentence_buf = ""
        try:
            async for token in llm_service.stream_response(transcript, conversation_history):
                await _send_json(ws, {"type": "llm.token", "text": token})
                sentence_buf += token
                full_response_parts.append(token)

                stripped = sentence_buf.strip()
                if stripped and len(stripped) > 5 and stripped[-1] in _SENTENCE_ENDERS:
                    await sentence_queue.put(sentence_buf)
                    sentence_buf = ""

            # Flush leftover text
            if sentence_buf.strip():
                await sentence_queue.put(sentence_buf)

        except Exception as exc:
            logger.error("LLM streaming error: %s", exc)
            await _send_json(ws, {"type": "error", "message": f"AI response failed: {exc}"})
        finally:
            # Signal TTS that no more sentences are coming
            await sentence_queue.put(None)

            # Send full response to client
            full_text = "".join(full_response_parts)
            await _send_json(ws, {"type": "llm.done", "text": full_text})

            # Update conversation history (in-memory, per session)
            if full_text:
                conversation_history.append({"role": "user", "parts": [transcript]})
                conversation_history.append({"role": "model", "parts": [full_text]})

    # ── Task B: read sentences → TTS → stream audio to client ───────────
    async def _tts_to_client() -> None:
        first_audio = True
        chunk_count = 0
        total_bytes = 0

        async def _sentence_gen():
            """Async generator that drains the sentence queue."""
            while True:
                sentence = await sentence_queue.get()
                if sentence is None:
                    logger.info("TTS sentence queue exhausted (None sentinel)")
                    break
                logger.info("TTS sentence dequeued: %.80s", sentence.strip())
                yield sentence

        try:
            async for audio_chunk in tts_service.stream_tts(_sentence_gen()):
                if first_audio:
                    logger.info("First TTS audio chunk received (%d bytes)", len(audio_chunk))
                    await _send_json(ws, {
                        "type": "tts.start",
                        "sampleRate": settings.SARVAM_TTS_SAMPLE_RATE,
                    })
                    first_audio = False
                chunk_count += 1
                total_bytes += len(audio_chunk)
                await ws.send_bytes(audio_chunk)

            logger.info("TTS streaming complete: %d chunks, %d bytes total", chunk_count, total_bytes)

        except Exception as exc:
            logger.error("TTS streaming error: %s", exc, exc_info=True)
            await _send_json(ws, {"type": "error", "message": f"Speech synthesis failed: {exc}"})
        finally:
            if chunk_count == 0:
                logger.warning("No audio chunks were produced by TTS")
            await _send_json(ws, {"type": "tts.done"})

    # ── Run both tasks concurrently ──────────────────────────────────────
    await asyncio.gather(_llm_to_sentences(), _tts_to_client())


# ═════════════════════════════════════════════════════════════════════════
#  WebSocket endpoint
# ═════════════════════════════════════════════════════════════════════════
@app.websocket("/ws/voice")
async def voice_websocket(ws: WebSocket) -> None:
    """
    Handle a full-duplex voice conversation session.

    Architecture (Hybrid VAD):
      - Client-side Silero VAD detects speech start → instant barge-in
      - Client streams raw PCM-16 audio continuously to this server
      - Server forwards audio to Deepgram Nova-3 for transcription
      - Deepgram's built-in endpointing detects end-of-turn
      - utterance_end event triggers the LLM → TTS pipeline
    """
    await ws.accept()
    logger.info("Client connected")

    # ── Per-session state ────────────────────────────────────────────────
    conversation_history: list[dict] = []
    pipeline_task: asyncio.Task | None = None

    # ── Per-session services ─────────────────────────────────────────────
    session_tts = SarvamTTS()
    session_stt = DeepgramStreamingSTT()

    # ── Deepgram callbacks (closures with access to session state) ────────

    async def on_interim_transcript(text: str) -> None:
        """Forward interim (partial) transcript to client for real-time display."""
        await _send_json(ws, {"type": "stt.interim", "text": text})

    async def on_final_transcript(text: str) -> None:
        """Forward finalized transcript segment to client."""
        await _send_json(ws, {"type": "stt.final", "text": text})

    async def on_utterance_end(transcript: str) -> None:
        """Deepgram detected end-of-turn — trigger the LLM → TTS pipeline."""
        nonlocal pipeline_task

        logger.info("Utterance complete → %s", transcript)

        # Tell client we have the final result (client stops streaming audio)
        await _send_json(ws, {"type": "stt.result", "text": transcript})

        # Cancel any running pipeline (e.g. from a previous turn)
        if pipeline_task and not pipeline_task.done():
            pipeline_task.cancel()
            try:
                await pipeline_task
            except (asyncio.CancelledError, Exception):
                pass
            # Reset TTS connection to drop any pending audio
            await session_tts.close()
            await session_tts.connect()

        # Launch LLM → TTS pipeline
        pipeline_task = asyncio.create_task(
            run_voice_pipeline(ws, transcript, conversation_history, session_tts)
        )

    # ── Main loop ────────────────────────────────────────────────────────
    try:
        # Connect TTS eagerly (it's used for every response)
        await session_tts.connect()
        # Connect STT eagerly since we stream continuously
        await session_stt.connect(
            on_interim=on_interim_transcript,
            on_final=on_final_transcript,
            on_utterance_end=on_utterance_end,
        )
        logger.info("STT & TTS WebSockets ready — waiting for speech")

        while True:
            message = await ws.receive()

            # ── Connection closed ────────────────────────────────────
            if message["type"] == "websocket.disconnect":
                break

            # ── JSON control messages ────────────────────────────────
            if "text" in message:
                data = json.loads(message["text"])
                msg_type = data.get("type", "")

                if msg_type == "speech.start":
                    # Client VAD detected speech — reset Deepgram transcript just in case
                    logger.info("Speech started (VAD)")
                    if session_stt.ws is not None:
                        session_stt.reset_transcript()

                elif msg_type == "interrupt":
                    # Client VAD detected barge-in
                    logger.info("Interrupt requested (barge-in)")
                    if pipeline_task and not pipeline_task.done():
                        pipeline_task.cancel()
                        try:
                            await pipeline_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        # Reset TTS to drop pending audio from old pipeline
                        await session_tts.close()
                        await session_tts.connect()

                    # Reset Deepgram for fresh session (drops old partial transcripts)
                    await session_stt.close()
                    await session_stt.connect(
                        on_interim=on_interim_transcript,
                        on_final=on_final_transcript,
                        on_utterance_end=on_utterance_end,
                    )
                    await _send_json(ws, {"type": "interrupted"})

                elif msg_type == "clear_history":
                    conversation_history.clear()
                    await _send_json(ws, {"type": "history_cleared"})
                    logger.info("Conversation history cleared")

                elif msg_type == "ping":
                    await _send_json(ws, {"type": "pong"})

            # ── Binary audio frames → forward to Deepgram ────────────
            elif "bytes" in message:
                # Log the very first chunk to confirm audio is arriving
                if not hasattr(ws, "_audio_received"):
                    logger.info("First client audio chunk received (%d bytes)", len(message["bytes"]))
                    ws._audio_received = True
                await session_stt.send_audio(message["bytes"])

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        if pipeline_task and not pipeline_task.done():
            pipeline_task.cancel()
            try:
                await pipeline_task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.gather(
            session_stt.close(),
            session_tts.close(),
        )
        logger.info("Connection cleaned up")


# ═════════════════════════════════════════════════════════════════════════
#  Health check
# ═════════════════════════════════════════════════════════════════════════
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "tts-speech-engine"}


# ═════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ═════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
        log_level="info",
    )
