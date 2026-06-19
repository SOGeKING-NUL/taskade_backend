"""
Sentiment service — cheap per-turn signal + periodic batch rollup.

Two halves, mirroring the reminder system's detection/delivery split:
  • note_sentiment()  — fire-and-forget after a turn: one fast SLM classification
    of the user's utterance → a MoodLog row. Cheap, never awaited inline.
  • rollup_sentiment() — periodic batch (driven by the scheduler): averages recent
    MoodLog rows and writes a one-line summary onto the profile. The expensive
    summarization runs here, NOT per message.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from db.session import async_session
from models.mood_log import MoodLog
import services.memory.profile_service as profile_service

logger = logging.getLogger(__name__)

# Fast/cheap path uses the Groq SLM; summary rollup uses the OpenRouter LLM.
_slm = AsyncOpenAI(api_key=settings.GROQ_API_KEY, base_url=settings.GROQ_BASE_URL)
_llm = AsyncOpenAI(api_key=settings.OPENROUTER_API_KEY, base_url=settings.OPENROUTER_BASE_URL)

_CLASSIFY_PROMPT = (
    "Classify the emotional tone of the user's message in a voice assistant. "
    "Reply with ONLY a JSON object: {\"score\": float between -1 and 1, "
    "\"note\": short reason}. -1 is very negative/frustrated, 0 neutral, +1 very "
    "positive/happy."
)

_SUMMARY_PROMPT = (
    "You are given a list of short mood notes from a user's recent interactions, "
    "with an average score. Write ONE concise sentence describing the user's "
    "recent mood and what's driving it, for the assistant to keep in mind. Be "
    "warm and factual; no preamble."
)


async def note_sentiment(user_id: str, user_text: str) -> None:
    """Classify one utterance and log it. Fire-and-forget."""
    if not user_text.strip():
        return
    try:
        resp = await _slm.chat.completions.create(
            model=settings.SLM_MODEL,
            messages=[
                {"role": "system", "content": _CLASSIFY_PROMPT},
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        score = float(data.get("score", 0.0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentiment classification failed: %s", exc)
        return

    score = max(-1.0, min(1.0, score))
    async with async_session() as session:
        session.add(MoodLog(user_id=user_id, score=score, note=data.get("note")))
        await session.commit()


async def rollup_sentiment(session: AsyncSession, user_id: str) -> bool:
    """Aggregate recent mood logs → profile sentiment. Returns True if updated."""
    since = datetime.now(timezone.utc) - timedelta(days=settings.SENTIMENT_WINDOW_DAYS)
    rows = (
        await session.execute(
            select(MoodLog)
            .where(MoodLog.user_id == user_id, MoodLog.created_at >= since)
            .order_by(MoodLog.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    if not rows:
        return False

    avg = sum(r.score for r in rows) / len(rows)
    notes = "; ".join(r.note for r in rows if r.note)[:1500]

    summary = ""
    try:
        resp = await _llm.chat.completions.create(
            model=settings.OPENROUTER_LLM_MODEL,
            messages=[
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": f"Average score: {avg:.2f}\nNotes: {notes}"},
            ],
            temperature=0.3,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentiment summary failed: %s", exc)
        summary = f"Recent mood averages {avg:+.2f}."

    await profile_service.set_sentiment(session, user_id, avg, summary)
    logger.info("sentiment rollup for %s → %.2f", user_id, avg)
    return True
