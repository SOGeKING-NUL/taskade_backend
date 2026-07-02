"""
UserProfile read/write — the structured half of Milestone 5 memory.

Pure DB logic (no LLM): cheap enough to read on the hot path at connect time.
"""

import logging
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.user import User
from models.user_profile import UserProfile
import services.tasks.task_service as task_service

logger = logging.getLogger(__name__)


async def ensure_profile(
    session: AsyncSession,
    user_id: str,
    display_name: str | None = None,
    email: str | None = None,
) -> UserProfile:
    """Fetch the profile, creating a default row (and the user) on first contact.

    Backfills `display_name` when it's missing and a value is supplied, so a
    profile row first created without identity (e.g. by a REST call) gets healed
    the next time a caller knows the name.
    """
    await task_service.ensure_user(session, user_id, display_name=display_name, email=email)
    profile = await session.get(UserProfile, user_id)
    if profile is None:
        profile = UserProfile(user_id=user_id, display_name=display_name)
        session.add(profile)
        await session.flush()
        logger.info("Created default profile for %s", user_id)
    elif display_name and not profile.display_name:
        profile.display_name = display_name
        await session.flush()
    return profile


async def sync_identity(
    session: AsyncSession,
    user_id: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
    timezone: str | None = None,
    locale: str | None = None,
) -> UserProfile:
    """Persist a user's identity at login time — one authoritative write of every
    known field, rather than lazily as features are used.

    Name/email come from the verified token claims; timezone/locale are supplied
    by the device (they aren't in the token). Fill semantics:
      • name/email — filled when missing (via `ensure_profile`/`ensure_user`),
        never clobbering an existing value.
      • timezone   — set from the device only while it's still the untouched
        default ("UTC"/empty), so a location-derived timezone is never overwritten.
      • locale     — set from the device when provided.
    """
    profile = await ensure_profile(session, user_id, display_name=display_name, email=email)

    if timezone and (profile.timezone in (None, "", "UTC")):
        profile.timezone = timezone
    if locale:
        profile.locale = locale
    await session.flush()
    logger.info("Synced identity for %s", user_id)
    return profile


async def set_profile_details(
    session: AsyncSession,
    user_id: str,
    *,
    location: str | None = None,
    timezone: str | None = None,
    display_name: str | None = None,
) -> UserProfile:
    """Persist stable personal details the user shares (location, tz, name)."""
    profile = await ensure_profile(session, user_id)
    if location is not None:
        profile.location = location
    if timezone is not None:
        profile.timezone = timezone
    if display_name is not None:
        profile.display_name = display_name
    await session.flush()
    logger.info("Updated profile details for %s", user_id)
    return profile


async def complete_onboarding(
    session: AsyncSession,
    user_id: str,
    *,
    display_name: str | None = None,
    location: str | None = None,
    timezone: str | None = None,
    daily_checkin_hour: int | None = None,
) -> UserProfile:
    """Finalise first-run onboarding — the user's own answers are AUTHORITATIVE.

    Unlike `ensure_*`/`sync_identity` (which only fill blanks), this OVERWRITES
    `display_name` with the confirmed value (so the OAuth-suggested name is
    replaced by what the user actually told us), sets location/timezone/check-in
    hour when provided, and flips `onboarding_complete` to True. The confirmed
    name is mirrored onto the `users` row too, so both stores agree.
    """
    profile = await ensure_profile(session, user_id)

    if display_name and display_name.strip():
        name = display_name.strip()
        profile.display_name = name
        # Mirror the confirmed name onto the users row so both stores agree.
        user = await session.get(User, user_id)
        if user is not None:
            user.display_name = name

    if location is not None and location.strip():
        profile.location = location.strip()
    if timezone is not None and timezone.strip():
        profile.timezone = timezone.strip()
    if daily_checkin_hour is not None and 0 <= daily_checkin_hour <= 23:
        profile.daily_checkin_hour = daily_checkin_hour

    profile.onboarding_complete = True
    await session.flush()
    logger.info("Onboarding complete for %s (name=%s)", user_id, profile.display_name)
    return profile


def checkin_hour(profile: UserProfile) -> int:
    """The user's daily refresh hour, falling back to the global default."""
    if profile.daily_checkin_hour is not None:
        return profile.daily_checkin_hour
    return settings.DAILY_CHECKIN_HOUR


async def mark_refreshed(session: AsyncSession, user_id: str, on: date) -> None:
    """Stamp that the daily research refresh ran for this local day."""
    profile = await ensure_profile(session, user_id)
    profile.last_refresh_on = on
    await session.flush()


async def mark_checked_in(session: AsyncSession, user_id: str, on: date) -> None:
    """Stamp that the daily check-in push was processed for this local day."""
    profile = await ensure_profile(session, user_id)
    profile.last_checkin_on = on
    await session.flush()
