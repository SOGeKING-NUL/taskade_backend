"""
Daily check-in composer — the user-facing text for the once-a-day proactive push.

Deterministic (no LLM on the fan-out path — keeps the scheduled sweep cheap and
immune to rate limits): it greets by time-of-day and summarizes the relevant
slice of the user's tasks. The slice adapts to the hour the user chose:

    morning / afternoon → what's still due TODAY  (+ overdue)
    evening / night     → what's coming TOMORROW  (+ overdue)

Returns None when there's nothing worth saying, so the sweep can skip sending an
empty check-in rather than nag.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from models.user_profile import UserProfile
import services.tasks.task_service as task_service
from utils import timez


def _greeting(hour: int) -> str:
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 21:
        return "Good evening"
    return "Hello"


def _titles(tasks, limit: int = 2) -> str:
    """Join up to `limit` task titles, with 'and N more' for the remainder."""
    names = [t.title for t in tasks if t.title]
    if not names:
        return ""
    if len(names) <= limit:
        return ", ".join(names)
    shown = ", ".join(names[:limit])
    extra = len(names) - limit
    return f"{shown}, and {extra} more"


async def build_checkin(
    session: AsyncSession, user_id: str, profile: UserProfile
) -> tuple[str, str] | None:
    """Build (title, body) for the daily check-in, or None if nothing to say."""
    tz = profile.timezone
    local = timez.now_local(tz)
    now_utc = datetime.now(timezone.utc)
    name = (profile.display_name or "").strip() or "there"
    greeting = _greeting(local.hour)

    # Choose the window: today (day-time) vs tomorrow (evening/night).
    evening = local.hour >= 17 or local.hour < 5
    if evening:
        start_local = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local.replace(hour=23, minute=59, second=59)
        window_label = "tomorrow"
        due_after = start_local.astimezone(timezone.utc)
    else:
        # Rest of today: from now until end of the local day.
        end_local = local.replace(hour=23, minute=59, second=59, microsecond=0)
        window_label = "today"
        due_after = now_utc
    due_before = end_local.astimezone(timezone.utc)

    window_tasks = await task_service.get_tasks(
        session, user_id, scope="all_active", due_after=due_after, due_before=due_before
    )
    overdue = await task_service.get_tasks(session, user_id, scope="overdue")

    # Nothing relevant → skip the check-in entirely.
    if not window_tasks and not overdue:
        return None

    parts: list[str] = []
    if window_tasks:
        n = len(window_tasks)
        noun = "task" if n == 1 else "tasks"
        parts.append(f"You have {n} {noun} {window_label} — {_titles(window_tasks)}.")
    else:
        parts.append(f"Nothing scheduled {window_label}.")

    if overdue:
        n = len(overdue)
        noun = "task" if n == 1 else "tasks"
        parts.append(f"{n} overdue {noun}: {_titles(overdue)}.")

    title = f"{greeting}, {name}"
    body = " ".join(parts)
    return title, body
