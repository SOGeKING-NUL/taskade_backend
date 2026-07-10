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
import random
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.config import settings
from db.session import async_session, init_db
import services.tasks.task_service as task_service
import services.memory.profile_service as profile_service
import services.memory.memory_service as memory_service
import services.engagement.engagement_service as engagement_service
import services.devices.device_service as device_service
import services.reminders.reminder_service as reminder_service
from services.auth.auth_service import authenticate_websocket, get_current_user_id, profile_fields
from services.scheduler.scheduler_service import start_scheduler, shutdown_scheduler, get_job_status
from services.voice.stt_deepgram import DeepgramStreamingSTT
from services.voice.tts import SarvamTTS
from services.ai.brain import GeminiBrain

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("engine")

# ── App & middleware ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    start_scheduler()  # read-only due-task detection sweep
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(title="TTS Speech Engine", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global services (stateless, shared across connections) ───────────────
# Single brain: Gemini answers, reasons, and calls the tools itself
# (create_task / query_tasks / update_task / update_task_status / research /
# update_profile). `research` is the only tool that reaches out to OpenRouter
# (GPT-4o-mini web search) under the hood.
brain_service = GeminiBrain()

# ── Sentence-boundary characters ────────────────────────────────────────
# Deliberately NOT ":"/";" — a colon after a markdown-style header/list line (e.g.
# "**August 2026:**", stray even with the no-markdown prompt rules) would end a
# "sentence" right there, flushing a garbage fragment to TTS as its own utterance.
# That produced the audible "ticking" bug: dozens of tiny flushes back to back.
_SENTENCE_ENDERS = frozenset(".!?।")

# Strips markdown syntax before text reaches TTS (spoken audio must never contain
# "**"/"#"/bullet markers — Sarvam vocalizes them as noise/clicks). Applied ONLY to
# the copy sent to TTS; the on-screen transcript keeps markdown so it still renders
# nicely in the client's message bubbles.
_MARKDOWN_STRIP_RE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|__(.+?)__|_(.+?)_|`(.+?)`")
_MARKDOWN_LINE_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+|^#{1,6}[ \t]+", re.MULTILINE)


def _for_speech(text: str) -> str:
    """Strip markdown emphasis/list/header syntax so TTS never has to vocalize it."""
    text = _MARKDOWN_LINE_RE.sub("", text)
    text = _MARKDOWN_STRIP_RE.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    return re.sub(r"\s+", " ", text).strip()

# Keep the in-memory chat prompt bounded on long sessions (messages, not turns).
_MAX_HISTORY_MESSAGES = 20
# Run the background memory-extraction pass once every N turns rather than every
# turn — firing an extra LLM call per turn inflated our per-minute API volume and
# tripped free-tier rate limits under rapid back-and-forth.
_LEARN_EVERY_TURNS = 3

# Short spoken acknowledgements played the moment a (slow) web `research` tool
# starts, so the user hears something while the lookup runs in the background
# instead of dead air. Kept to one per turn.
_RESEARCH_FILLERS = [
    "Let me look that up for you.",
    "Give me a moment to check on that.",
    "Sure, let me research that real quick.",
    "One moment while I look into that.",
]


# ═════════════════════════════════════════════════════════════════════════
#  Helper
# ═════════════════════════════════════════════════════════════════════════
async def _send_json(ws: WebSocket, data: dict) -> None:
    """Send a JSON text frame — silently ignores closed connections."""
    try:
        await ws.send_json(data)
    except Exception:  # noqa: BLE001
        pass


def _extract_presentable_metadata(tool_name: str, result: dict, args: dict) -> dict | None:
    """Pull user-facing structured data (links, findings, dates) from a tool
    result for display as a separate UI card — deterministic, not dependent
    on the model choosing to recite anything correctly.

    Deliberately narrow: the card should appear ONLY when the user explicitly
    asked about a specific, already-tracked task — never as a side effect of
    research running in the background or a task simply being created/updated.
    That means exactly one case: `query_tasks` called with
    `scope == "specific_task"`. Everything else returns None.

    Returns None when there's nothing worth surfacing.
    """
    if tool_name != "query_tasks" or args.get("scope") != "specific_task":
        return None

    tasks_data = result.get("tasks") or []
    items = []
    for t in tasks_data:
        if not isinstance(t, dict):
            continue
        item: dict = {"title": t.get("title", "")}
        if t.get("due_at"):
            item["due_at"] = t["due_at"]
        ctx = t.get("context") or {}
        for key in ("research", "research_refresh"):
            blob = ctx.get(key) or {}
            if isinstance(blob, dict):
                if blob.get("links"):
                    item.setdefault("links", []).extend(blob["links"])
                if blob.get("summary"):
                    item["summary"] = blob["summary"]
        if len(item) > 1:  # more than just title
            items.append(item)

    return {"tasks": items} if items else None


async def _memory_context(session_context: dict, transcript: str) -> str:
    """
    Build a short system block of what we know about the user, injected into the
    brain's prompt each turn — so `research` / `create_task` run with the user's
    known prefs/facts in view.

    Pulls from three sources (deliberately small — a big memory block is what
    bloated the prompt and tripped rate limits before):
      1. Profile fields (from session context, loaded at connect time).
      2. Semantic memory: mem0 vector search keyed on THIS turn's transcript, so
         the facts injected are the ones actually relevant to what the user is
         talking about now — not every fact we've ever stored.
      3. Tasks deterministically matched against THIS turn's transcript — so a
         task's stored research/links are already in front of the model before
         it ever decides whether to call `research` or `query_tasks`. A fixed DB
         lookup, not a model judgment, so a routing mistake can't skip it.
    """
    user_id = session_context["user_id"]
    profile = session_context.get("profile") or {}

    lines: list[str] = []
    if profile.get("display_name"):
        lines.append(f"User's name: {profile['display_name']}.")
    if profile.get("location"):
        lines.append(f"Location: {profile['location']}.")
    if profile.get("timezone"):
        lines.append(f"Timezone: {profile['timezone']}.")
    prefs = profile.get("preferences") or {}
    if prefs:
        lines.append(f"Preferences: {prefs}.")

    try:
        # Semantic memory (mem0) — top facts relevant to what was just said.
        facts = await memory_service.recall(user_id, query=transcript, limit=5)
        if facts:
            lines.append("Relevant facts about the user:")
            lines += [f"- {f}" for f in facts]

        async with async_session() as session:
            # ── Deterministic task pre-retrieval ─────────────────────────
            # Match THIS turn's words against the user's active task titles and
            # surface any hits — INCLUDING their stored research/links — so the
            # answer to "what's the link for X" is already in the prompt before
            # the model picks a tool. A fixed DB lookup, never a model judgment.
            relevant = await task_service.find_relevant_tasks(
                session, user_id, transcript, limit=3
            )
            if relevant:
                lines.append(
                    "Existing tasks that may relate to what the user just said — "
                    "CHECK THESE before calling research or creating anything; the "
                    "answer (link, date, details) may already be here:"
                )
                for t in relevant:
                    due = t.due_at.strftime("%d %b %Y") if t.due_at else "no due date"
                    lines.append(f'- "{t.title}" (status: {t.status}, {due})')
                    ctx = t.context or {}
                    for key in ("research", "research_refresh"):
                        blob = ctx.get(key) or {}
                        if not isinstance(blob, dict):
                            continue
                        if blob.get("summary"):
                            lines.append(f"    findings: {blob['summary']}")
                        if blob.get("note"):
                            lines.append(f"    update: {blob['note']}")
                        for link in (blob.get("links") or []):
                            if isinstance(link, dict) and link.get("url"):
                                label = link.get("label") or link["url"]
                                lines.append(f"    link: {label} — {link['url']}")
                            elif isinstance(link, str) and link:
                                lines.append(f"    link: {link}")

    except Exception as exc:  # noqa: BLE001
        logger.warning("memory context build failed: %s", exc)

    if not lines:
        return ""
    return "What you know about the user (use it to personalize, don't recite it):\n" + "\n".join(lines)


async def _learn_from_turns(user_id: str, exchanges: list[tuple[str, str]]) -> None:
    """
    Background batch learn — runs every few turns instead of every turn.

    Folds several exchanges into ONE memory-extraction call, so learning costs
    one LLM call per N turns instead of one per turn. Fire-and-forget; the
    memory service swallows its own exceptions.
    """
    if not exchanges:
        return
    user_text = "\n".join(u for u, _ in exchanges if u)
    assistant_text = "\n".join(a for _, a in exchanges if a)
    await memory_service.remember(user_id, user_text, assistant_text)


# ═════════════════════════════════════════════════════════════════════════
#  Voice pipeline (LLM → TTS only — STT is handled by Deepgram callbacks)
# ═════════════════════════════════════════════════════════════════════════
async def run_voice_pipeline(
    ws: WebSocket,
    transcript: str,
    conversation_history: list[dict],
    tts_service: SarvamTTS,
    session_context: dict,
) -> None:
    """
    SLM (reason + tool-call) → TTS cascade for one user turn.

    Receives a pre-computed transcript (from Deepgram utterance_end). The Groq
    SLM holds the conversation, answers directly, and calls tools itself
    (create_task / query_tasks / update_task_status / research) as needed; its
    final spoken answer streams sentence-by-sentence into the TTS path.
    Conversation history is stored in OpenAI message format.
    """

    await _send_json(ws, {"type": "processing", "stage": "slm"})

    sentence_queue: asyncio.Queue[str | None] = asyncio.Queue()
    full_response_parts: list[str] = []

    async def _emit(token: str) -> str:
        """Send a text token to client + buffer it; returns nothing (uses closure)."""
        await _send_json(ws, {"type": "llm.token", "text": token})
        full_response_parts.append(token)
        return token

    # ── Task A: SLM reasons + calls tools → spoken sentences ─────────────
    async def _produce_text() -> None:
        sentence_buf = ""

        async def _push_sentences(buf: str) -> str:
            stripped = buf.strip()
            if stripped and len(stripped) > 5 and stripped[-1] in _SENTENCE_ENDERS:
                spoken = _for_speech(buf)
                if spoken:
                    await sentence_queue.put(spoken)
                return ""
            return buf

        try:
            # Single brain: the SLM holds the whole conversation and calls the
            # tools itself. Build its message list (system prompt + what we know
            # about the user + prior history + this turn), then run the tool loop.
            # `run_conversation` only yields SPOKEN text from a round that makes no
            # tool calls, so a pre-research/"thinking" answer is never voiced.
            messages = [{"role": "system", "content": brain_service.system_prompt}]
            memory_msg = await _memory_context(session_context, transcript)
            if memory_msg:
                messages.append({"role": "system", "content": memory_msg})
            messages += list(conversation_history)
            messages.append({"role": "user", "content": transcript})

            spoke_filler = False
            # Hard per-step wall-clock backstop. `GeminiBrain`'s own client
            # timeout/retries normally surface a failure as a graceful
            # `openai.APIError` (caught in run_tool_loop, yields an apology) — but
            # Gemini's free tier has shown transient 503s, and a hang from ANY
            # cause (a dead connection that never raises, an unexpectedly long
            # retry chain) must never leave the pipeline silently "stuck" with no
            # audio cue. Bounding each individual event-yield (not the whole
            # multi-round loop) means a legitimately long multi-tool turn isn't
            # cut short, but a single stalled step is.
            brain_events = brain_service.run_conversation(messages, session_context).__aiter__()
            while True:
                try:
                    ev = await asyncio.wait_for(brain_events.__anext__(), timeout=25.0)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.error("Brain turn timed out after 25s — unsticking the pipeline")
                    if sentence_buf.strip():
                        spoken = _for_speech(sentence_buf)
                        if spoken:
                            await sentence_queue.put(spoken)
                        sentence_buf = ""
                    apology = "Sorry, that's taking too long — could you try again?"
                    await _emit(apology)
                    await sentence_queue.put(apology)
                    break

                if ev["type"] == "text":
                    await _emit(ev["text"])
                    sentence_buf += ev["text"]
                    sentence_buf = await _push_sentences(sentence_buf)
                elif ev["type"] == "tool.start":
                    await _send_json(ws, {"type": "tool.start", "name": ev["name"]})
                    # Speak a short filler while a slow web lookup runs, so the user
                    # isn't left in silence. Pushed straight to TTS (not via _emit)
                    # so it's spoken but NOT recorded as part of the answer text or
                    # history. Once per turn, research-only.
                    if ev["name"] == "research" and not spoke_filler:
                        spoke_filler = True
                        if sentence_buf.strip():
                            spoken = _for_speech(sentence_buf)
                            if spoken:
                                await sentence_queue.put(spoken)
                            sentence_buf = ""
                        await sentence_queue.put(random.choice(_RESEARCH_FILLERS))
                elif ev["type"] == "tool.result":
                    await _send_json(ws, {
                        "type": "tool.result",
                        "name": ev["name"],
                        "ok": ev["ok"],
                        "summary": ev.get("summary", ""),
                    })
                    # Dispatch structured metadata as a separate channel for
                    # the frontend to render as a clickable card — never spoken.
                    metadata = _extract_presentable_metadata(ev["name"], ev, ev.get("args") or {})
                    if metadata:
                        await _send_json(ws, {
                            "type": "metadata",
                            "tool": ev["name"],
                            "data": metadata,
                        })

            # Flush leftover text
            if sentence_buf.strip():
                spoken = _for_speech(sentence_buf)
                if spoken:
                    await sentence_queue.put(spoken)

        except Exception as exc:
            logger.error("LLM/SLM streaming error: %s", exc, exc_info=True)
            await _send_json(ws, {"type": "error", "message": f"AI response failed: {exc}"})
        finally:
            # Signal TTS that no more sentences are coming
            await sentence_queue.put(None)

            # Send full response to client
            full_text = "".join(full_response_parts)
            await _send_json(ws, {"type": "llm.done", "text": full_text})

            # Update conversation history (in-memory, per session, OpenAI format)
            if full_text:
                conversation_history.append({"role": "user", "content": transcript})
                conversation_history.append({"role": "assistant", "content": full_text})
                # Bound the prompt so a long session can't grow it unboundedly.
                if len(conversation_history) > _MAX_HISTORY_MESSAGES:
                    del conversation_history[:-_MAX_HISTORY_MESSAGES]

                # Milestone 5 — learn from the turn WITHOUT blocking it AND without
                # firing 2 extra LLM calls every turn. Buffer the exchange and run a
                # single batched learn pass every _LEARN_EVERY_TURNS turns. Still
                # fire-and-forget: a slow extraction must never delay the next turn.
                user_id = session_context["user_id"]
                learn_buf = session_context.setdefault("_learn_buf", [])
                learn_buf.append((transcript, full_text))
                session_context["turn_count"] = session_context.get("turn_count", 0) + 1
                if session_context["turn_count"] % _LEARN_EVERY_TURNS == 0:
                    batch = list(learn_buf)
                    learn_buf.clear()
                    asyncio.create_task(_learn_from_turns(user_id, batch))

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
    await asyncio.gather(_produce_text(), _tts_to_client())


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
    # Verify the Supabase JWT (carried as ?token=, since browsers can't attach
    # custom headers to a WS upgrade request) BEFORE accepting the connection.
    claims = await authenticate_websocket(ws)
    if claims is None:
        return  # already closed by authenticate_websocket

    await ws.accept()
    user_id = claims["sub"]
    logger.info("Client connected (user=%s)", user_id)

    # ── Per-session state ────────────────────────────────────────────────
    conversation_history: list[dict] = []
    pipeline_task: asyncio.Task | None = None
    session_context: dict = {"user_id": user_id}

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

    async def on_stt_reconnect() -> None:
        """STT socket dropped and is being re-established — tell the client."""
        logger.warning("STT reconnecting — notifying client")
        await _send_json(ws, {"type": "stt.reconnecting"})

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

        # Launch SLM/LLM → TTS pipeline
        pipeline_task = asyncio.create_task(
            run_voice_pipeline(ws, transcript, conversation_history, session_tts, session_context)
        )

    # ── Main loop ────────────────────────────────────────────────────────
    try:
        # Load the user's profile once into session context (Milestone 5) — the
        # only memory read on the connect path; per-turn recall is separate.
        # On first contact, seed display_name/email from the Google claims.
        try:
            fields = profile_fields(claims)
            async with async_session() as session:
                profile = await profile_service.ensure_profile(session, user_id, **fields)
                session_context["profile"] = profile.to_context()
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("profile load failed: %s", exc)

        # Connect TTS eagerly (it's used for every response)
        await session_tts.connect()
        # Connect STT eagerly since we stream continuously
        await session_stt.connect(
            on_interim=on_interim_transcript,
            on_final=on_final_transcript,
            on_utterance_end=on_utterance_end,
            on_reconnect=on_stt_reconnect,
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
                        on_reconnect=on_stt_reconnect,
                    )
                    await _send_json(ws, {"type": "interrupted"})

                elif msg_type == "clear_history":
                    conversation_history.clear()
                    await _send_json(ws, {"type": "history_cleared"})
                    logger.info("Conversation history cleared")

                elif msg_type == "location":
                    # Browser-resolved location (see POST /profile/location for the
                    # durable save). This just makes it live in THIS session's
                    # context so the very turn after granting is already location-aware.
                    loc = (data.get("location") or "").strip()
                    if loc:
                        prof = dict(session_context.get("profile") or {})
                        prof["location"] = loc
                        session_context["profile"] = prof
                        logger.info("Session location set live: %s", loc)

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
#  Profile REST (location capture — frontend resolves it once, we persist it)
# ═════════════════════════════════════════════════════════════════════════
class LocationIn(BaseModel):
    location: str
    timezone: str | None = None


@app.get("/profile")
async def get_profile(user_id: str = Depends(get_current_user_id)):
    """Return the user's profile context — the frontend checks `location` to
    decide whether it still needs to ask the browser for it (so we never
    re-prompt once it's stored)."""
    async with async_session() as session:
        profile = await profile_service.ensure_profile(session, user_id)
        ctx = profile.to_context()
        await session.commit()
    return {"profile": ctx}


@app.post("/profile/location")
async def set_profile_location(
    body: LocationIn, user_id: str = Depends(get_current_user_id)
):
    """Durably store a browser-resolved location so we don't ask again."""
    location = body.location.strip()
    if not location:
        return {"ok": False, "error": "empty_location"}
    async with async_session() as session:
        profile = await profile_service.set_profile_details(
            session, user_id, location=location, timezone=(body.timezone or None)
        )
        ctx = profile.to_context()
        await session.commit()
    logger.info("Stored location for %s: %s", user_id, location)
    return {"ok": True, "profile": ctx}


# ═════════════════════════════════════════════════════════════════════════
#  Engagement (on-demand greeting — app-open / notifications; NOT the hot path)
# ═════════════════════════════════════════════════════════════════════════
@app.get("/engagement/greeting")
async def engagement_greeting(user_id: str = Depends(get_current_user_id)):
    """A short, personalized hype/greeting built on demand from the user's recent
    memories + upcoming tasks. Called at app-open or by the (future) notification
    layer — never on the conversation path."""
    greeting = await engagement_service.generate_greeting(user_id)
    return {"greeting": greeting}


# ═════════════════════════════════════════════════════════════════════════
#  Tasks REST (read-only — powers the frontend test panel)
# ═════════════════════════════════════════════════════════════════════════
@app.get("/tasks")
async def list_tasks(user_id: str = Depends(get_current_user_id)):
    """Return all tasks for the authenticated user, with parent titles, for the UI panel."""
    async with async_session() as session:
        tasks = await task_service.get_tasks(session, user_id, scope="all")
        # Filter out cancelled/deleted tasks for the user interface views
        tasks = [t for t in tasks if t.status != "cancelled"]
        out = []
        for t in tasks:
            brief = t.to_brief()
            brief["parent_title"] = None
            if t.parent_id:
                parent = next((p for p in tasks if p.id == t.parent_id), None)
                brief["parent_title"] = parent.title if parent else None
            out.append(brief)
    return {"tasks": out}


@app.delete("/tasks/{id}")
async def delete_task(id: str, user_id: str = Depends(get_current_user_id)):
    """Soft-delete a task by updating its status to cancelled."""
    async with async_session() as session:
        task, _ = await task_service.update_status(
            session, user_id, id, "cancelled"
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        await session.commit()
    return {"ok": True, "id": task.id}


# ═════════════════════════════════════════════════════════════════════════
#  Reminders REST (delivery half of Milestone 4)
# ═════════════════════════════════════════════════════════════════════════
@app.get("/reminders/due")
async def due_reminders(user_id: str = Depends(get_current_user_id)):
    """
    Deliver any due-and-unannounced reminders for the authenticated user.

    Calling this IS the act of delivery: it atomically fetches due tasks and
    marks them reminded, so a second call returns nothing. Invoked "at necessary
    times" by an external caller (session-start hook, mobile app, manual test) —
    deliberately NOT auto-triggered by the websocket. The scheduler only detects
    (read-only); this endpoint is the only thing that consumes/marks reminders.
    """
    async with async_session() as session:
        due, message = await task_service.consume_due_reminders(session, user_id)
        briefs = [t.to_brief() for t in due]
        await session.commit()
    if due:
        logger.info("Delivered %d due reminder(s) via /reminders/due", len(due))
    return {"count": len(due), "tasks": briefs, "message": message}


# ═════════════════════════════════════════════════════════════════════════
#  Push notifications — device-token registration + delivery acknowledgement
# ═════════════════════════════════════════════════════════════════════════
class DeviceTokenIn(BaseModel):
    token: str
    platform: str | None = "android"


@app.post("/devices/register")
async def register_device(
    body: DeviceTokenIn, user_id: str = Depends(get_current_user_id)
):
    """Register (or refresh) the caller's FCM device token so the delivery sweep
    can push reminders to it. Idempotent by token."""
    token = body.token.strip()
    if not token:
        return {"ok": False, "error": "empty_token"}
    async with async_session() as session:
        await device_service.register(session, user_id, token, body.platform or "android")
        await session.commit()
    return {"ok": True}


@app.post("/devices/unregister")
async def unregister_device(
    body: DeviceTokenIn, user_id: str = Depends(get_current_user_id)
):
    """Drop a device token (e.g. on logout) so it stops receiving pushes.

    Uses POST (not DELETE-with-path) because FCM tokens contain characters that
    are awkward to carry safely in a URL path segment.
    """
    token = body.token.strip()
    if not token:
        return {"ok": False, "error": "empty_token"}
    async with async_session() as session:
        removed = await device_service.unregister(session, token)
        await session.commit()
    return {"ok": True, "removed": removed}


@app.post("/reminders/{reminder_id}/delivered")
async def reminder_delivered(
    reminder_id: str, user_id: str = Depends(get_current_user_id)
):
    """Mobile acknowledgement that a reminder notification actually surfaced —
    end-to-end delivery proof (sets `delivered_at`). Scoped to the owner."""
    async with async_session() as session:
        ok = await reminder_service.mark_delivered(session, user_id, reminder_id)
        await session.commit()
    return {"ok": ok}


# ═════════════════════════════════════════════════════════════════════════
#  Scheduler & research-retry debug endpoint
# ═════════════════════════════════════════════════════════════════════════
@app.get("/debug/scheduler")
async def scheduler_debug(user_id: str = Depends(get_current_user_id)):
    """Return APScheduler job status + the calling user's research-retry
    schedule.  Per-user scoped — no cross-user data exposure.  Purely
    read-only introspection for the debug panel."""
    jobs = get_job_status()
    async with async_session() as session:
        research_schedule = await task_service.get_research_schedule(session, user_id)
    return {"jobs": jobs, "research_schedule": research_schedule}


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
