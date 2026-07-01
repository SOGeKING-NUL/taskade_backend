"""
Device token registry — one row per device the user can receive push on.

The mobile app reports its FCM registration token here on login / token
rotation; the delivery sweep reads these to know where to send a user's
reminders. One user may have several rows (phone + tablet); a token is globally
unique (`UNIQUE(token)`) so registering an existing token upserts rather than
duplicating, and re-registering a token that moved to another account simply
re-points its `user_id`.

Tokens that FCM reports as unregistered/invalid at send time are pruned here, so
the table never accumulates dead endpoints.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DeviceToken(Base):
    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_device_token"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True, nullable=False
    )
    token: Mapped[str] = mapped_column(String, nullable=False)
    platform: Mapped[str] = mapped_column(String, default="android", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
