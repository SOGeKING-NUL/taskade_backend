"""
Device-token registry service — the push delivery surface's address book.

The mobile app calls `register` on login and whenever FCM rotates its token;
the delivery sweep calls `tokens_for` to learn where to send, and `prune` to
drop a token FCM reported as dead. All functions take a session and leave the
commit to the caller, mirroring `task_service`.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.device_token import DeviceToken
import services.tasks.task_service as task_service

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def register(
    session: AsyncSession,
    user_id: str,
    token: str,
    platform: str = "android",
) -> DeviceToken:
    """Upsert a device token for a user.

    Idempotent by token (which is globally unique): re-registering the same token
    just refreshes `last_seen_at`; a token that moved to a different account is
    re-pointed at the new `user_id` rather than duplicated.
    """
    # Ensure the user row exists (the token FK requires it) — registration can
    # arrive before any task has created the user.
    await task_service.ensure_user(session, user_id)

    existing = (
        await session.execute(select(DeviceToken).where(DeviceToken.token == token))
    ).scalar_one_or_none()

    if existing is not None:
        existing.user_id = user_id
        existing.platform = platform
        existing.last_seen_at = _now()
        await session.flush()
        return existing

    row = DeviceToken(user_id=user_id, token=token, platform=platform, last_seen_at=_now())
    session.add(row)
    await session.flush()
    logger.info("device token registered for %s (%s)", user_id, platform)
    return row


async def unregister(session: AsyncSession, token: str) -> int:
    """Remove a token (e.g. on logout). Returns rows deleted."""
    result = await session.execute(delete(DeviceToken).where(DeviceToken.token == token))
    return result.rowcount or 0


async def prune(session: AsyncSession, token: str) -> int:
    """Drop a token FCM reported as unregistered/invalid. Returns rows deleted."""
    result = await session.execute(delete(DeviceToken).where(DeviceToken.token == token))
    if result.rowcount:
        logger.info("pruned dead device token")
    return result.rowcount or 0


async def tokens_for(session: AsyncSession, user_id: str) -> list[str]:
    """All registered device tokens for a user (every device they're signed in on)."""
    rows = (
        await session.execute(
            select(DeviceToken.token).where(DeviceToken.user_id == user_id)
        )
    ).scalars().all()
    return list(rows)
