"""
Engagement service — on-demand, OFF the hot path.

Generates a short, warm, personalized greeting for app-open / notification
moments ("your marathon's next week — how's training going?"). It reasons over
the data we already keep — the user's profile, recent remembered facts, and
upcoming/active tasks — with a single LLM call. This is never used in the live
conversation loop, so latency here is fine.
"""

import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

from core.config import settings
from db.session import async_session
import services.memory.profile_service as profile_service
import services.memory.memory_service as memory_service
import services.tasks.task_service as task_service

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
    "the user opens the app. Use what you know about them and their upcoming "
    "tasks to say something specific and encouraging — acknowledge a notable "
    "goal or an approaching deadline ('Your marathon's next week — how's "
    "training going?'). 1-2 sentences, friendly and natural, no markdown, "
    "no lists. If there's nothing notable, a brief warm hello is fine. "
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

