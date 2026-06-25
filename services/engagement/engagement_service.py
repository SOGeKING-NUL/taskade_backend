"""
Engagement service — on-demand, OFF the hot path.

Generates a short, warm, personalized greeting for app-open / notification
moments ("your marathon's next week — how's training going?"). It reasons over
the data we already keep semantically — the user's profile, recent remembered
facts, upcoming/active tasks, knowledge-graph reflections, and mood signals —
with a single LLM call. This is never used in the live conversation loop, so
latency here is fine.
"""

import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

from core.config import settings
from db.session import async_session
import services.memory.profile_service as profile_service
import services.memory.memory_service as memory_service
import services.tasks.task_service as task_service
import services.memory.reflection_service as reflection_service

logger = logging.getLogger(__name__)

# Off the hot path, but still bounded so a hung call can't hang the caller.
_client = AsyncOpenAI(
    api_key=settings.GROQ_API_KEY,
    base_url=settings.GROQ_BASE_URL,
    timeout=30.0,
    max_retries=2,
)

_SYSTEM_PROMPT = (
    "You write ONE short, warm spoken greeting for a voice assistant, shown when "
    "the user opens the app. Use what you know about them, their upcoming tasks, "
    "and recent STATE CHANGES (reflections) to say something specific and "
    "encouraging — acknowledge a notable goal, an approaching deadline, or "
    "something that changed recently ('We haven't talked about the JLPT in a "
    "while — still on track?'). 1-2 sentences, friendly and natural, no markdown, "
    "no lists. If a topic has negative sentiment, be delicate — don't force it. "
    "If there's nothing notable, a brief warm hello is fine. "
    "NEVER psychoanalyze the user or claim to know how they feel."
)


async def generate_greeting(user_id: str) -> str:
    """Build a personalized engagement greeting from profile + memories + tasks
    + knowledge graph reflections + mood signals.

    Returns "" on failure (caller can fall back to a static hello).
    """
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")

    async with async_session() as session:
        profile = await profile_service.ensure_profile(session, user_id)
        name, location = profile.display_name, profile.location
        facts = await memory_service.recall(session, user_id)
        tasks = await task_service.get_tasks(session, user_id, scope="all_active")

        # ── Knowledge graph context ──────────────────────────────────
        reflections = await reflection_service.get_recent_reflections(
            session, user_id, limit=5
        )
        mood = await reflection_service.get_recent_mood_summary(
            session, user_id, limit=5
        )

        await session.commit()

    task_lines = []
    for t in tasks:
        due = t.due_at.strftime("%d %b %Y") if t.due_at else "no date"
        task_lines.append(f"- {t.title} (due {due}, status {t.status})")

    parts = [f"Today is {today}."]
    if name:
        parts.append(f"User's name: {name}.")
    if location:
        parts.append(f"Location: {location}.")
    if facts:
        parts.append("Known about them:\n" + "\n".join(f"- {f}" for f in facts))
    parts.append(
        "Upcoming/active tasks:\n" + "\n".join(task_lines)
        if task_lines else "They have no active tasks right now."
    )

    # Add reflections (state-delta observations).
    if reflections:
        parts.append(
            "Recent state changes (use these for a contextual greeting):\n"
            + "\n".join(f"- {r.content}" for r in reflections)
        )

    # Add mood signals (tone guidance, never spoken).
    if mood:
        parts.append(
            "Topic sentiment (adjust your tone, never say this aloud):\n"
            + "\n".join(f"- {m['entity']}: {m['label']} ({m['valence']:+.1f})" for m in mood)
        )

    try:
        resp = await _client.chat.completions.create(
            model=settings.SLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            temperature=0.6,
        )
        greeting = (resp.choices[0].message.content or "").strip()
        logger.info("Engagement greeting for %s: %.80s", user_id, greeting)
        return greeting
    except Exception as exc:  # noqa: BLE001
        logger.warning("engagement greeting failed: %s", exc)
        return ""

