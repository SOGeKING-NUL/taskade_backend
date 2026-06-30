"""
Timezone helpers — keep the app UTC internally, IST (or the configured default)
at the human boundaries.

Storage and all comparisons stay tz-aware UTC. These helpers exist only for the
two edges where local time matters:

  • interpreting a user-spoken clock time ("8pm") that arrived without an offset,
  • formatting a datetime for notification copy the user reads.

A per-user `profile.timezone` (when set) should be passed as `tz_name` to
override the configured default (`settings.DEFAULT_TIMEZONE`, e.g. Asia/Kolkata).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import settings


def _zone(tz_name: str | None = None) -> ZoneInfo:
    """Resolve a timezone, falling back to the configured default, then UTC —
    never raises (a bad IANA name must not crash the task/notification path)."""
    for candidate in (tz_name, settings.DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def now_local(tz_name: str | None = None) -> datetime:
    """Current time in the configured/overridden local zone."""
    return datetime.now(_zone(tz_name))


def ensure_aware(dt: datetime | None, tz_name: str | None = None) -> datetime | None:
    """Coerce a datetime to tz-aware: a NAIVE value is interpreted as LOCAL time
    (the configured default, e.g. IST) — so "2026-06-29T20:00:00" means 8pm IST,
    not 8pm UTC. An already-aware value is returned unchanged."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_zone(tz_name))
    return dt


def to_local(dt: datetime | None, tz_name: str | None = None) -> datetime | None:
    """Convert an aware datetime into the local zone for display."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_zone(tz_name))


def fmt_clock(dt: datetime | None, tz_name: str | None = None) -> str:
    """Format an aware datetime as a friendly local clock time, e.g. '8:00 PM'."""
    local = to_local(dt, tz_name)
    if local is None:
        return ""
    # %I is zero-padded and platform-dependent for the no-pad form, so strip it
    # manually rather than rely on %-I / %#I (not portable across OSes).
    return local.strftime("%I:%M %p").lstrip("0")


def tz_label(tz_name: str | None = None) -> str:
    """Short zone label for prompts/copy, e.g. 'IST'."""
    return now_local(tz_name).tzname() or "UTC"
