"""
UserProfile — the small, structured, cheap-to-read half of Milestone 5 memory.

Read once per connection into session context (the only memory read allowed on
the hot path). Holds identity (name/location/timezone/locale), free-form
`preferences`, and the per-user daily check-in hour that drives the daily
research-refresh job.

Free-form extracted facts live separately in `user_memories` (see
models/user_memory.py) — two distinct stores, not one blob.
"""

from datetime import datetime, date, timezone

from sqlalchemy import String, Integer, Boolean, Date, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), primary_key=True
    )

    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Free-text location (city/region/country), e.g. "Delhi, India" — used to
    # personalize answers and research (local exam centres, deadlines, prices).
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    # IANA tz name, e.g. "Asia/Kolkata" — used to resolve the daily check-in hour.
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    locale: Mapped[str] = mapped_column(String, default="en-IN")

    # Hour-of-day (0-23, in the user's timezone) at which the daily research
    # refresh runs. Null → use the global DAILY_CHECKIN_HOUR default.
    daily_checkin_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # First-run onboarding gate. False until the user completes (or skips) the
    # onboarding flow that captures their preferred name, location, and check-in
    # hour. Existing rows default to False so they get onboarded on next open.
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Guards the daily refresh against running twice in one local day.
    last_refresh_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Guards the daily check-in push against firing twice in one local day.
    last_checkin_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Misc structured preferences (voice, verbosity, topics of interest, …).
    preferences: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_context(self) -> dict:
        """Compact dict folded into the per-session context at connect time."""
        return {
            "display_name": self.display_name,
            "location": self.location,
            "timezone": self.timezone,
            "locale": self.locale,
            "daily_checkin_hour": self.daily_checkin_hour,
            "onboarding_complete": self.onboarding_complete,
            "preferences": self.preferences or {},
        }
