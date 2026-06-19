"""
UserProfile read/write — the structured half of Milestone 5 memory.

Pure DB logic (no LLM): cheap enough to read on the hot path at connect time.
"""

import logging
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.user_profile import UserProfile
import services.tasks.task_service as task_service

logger = logging.getLogger(__name__)


async def ensure_profile(
    session: AsyncSession,
    user_id: str,
    display_name: str | None = None,
    email: str | None = None,
) -> UserProfile:
    """Fetch the profile, creating a default row (and the user) on first contact."""
    await task_service.ensure_user(session, user_id, display_name=display_name, email=email)
    profile = await session.get(UserProfile, user_id)
    if profile is None:
        profile = UserProfile(user_id=user_id, display_name=display_name)
        session.add(profile)
        await session.flush()
        logger.info("Created default profile for %s", user_id)
    return profile


def checkin_hour(profile: UserProfile) -> int:
    """The user's daily refresh hour, falling back to the global default."""
    if profile.daily_checkin_hour is not None:
        return profile.daily_checkin_hour
    return settings.DAILY_CHECKIN_HOUR


async def set_sentiment(
    session: AsyncSession, user_id: str, score: float, summary: str
) -> None:
    """Persist a rolled-up sentiment snapshot (called by the batch rollup)."""
    profile = await ensure_profile(session, user_id)
    profile.sentiment_score = score
    profile.sentiment_summary = summary
    await session.flush()


async def mark_refreshed(session: AsyncSession, user_id: str, on: date) -> None:
    """Stamp that the daily research refresh ran for this local day."""
    profile = await ensure_profile(session, user_id)
    profile.last_refresh_on = on
    await session.flush()
