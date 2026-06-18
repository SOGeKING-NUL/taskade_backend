"""
Periodic due-task detection (Milestone 4 — proactive half).

This is the **detection** half of the reminder system, and it is deliberately
decoupled from **delivery**:

    detection  (here)              delivery  (GET /reminders/due)
    ─────────────────────          ──────────────────────────────
    APScheduler ticks every        called on-demand "at necessary times"
    REMINDER_SWEEP_SECONDS         (session start, mobile app, manual test)
    read-only: just logs what      atomically fetches + marks tasks reminded
    is due — never mutates         (task_service.consume_due_reminders)

The two never call each other. If both marked tasks as reminded, whichever ran
first would silently swallow the reminder before the other delivered it — so the
"mark reminded" side-effect lives exclusively in the pull-based API. The sweep is
a heartbeat / future hook point for when a real outbound push/call channel exists
(mobile, WebRTC — out of scope now).

One single recurring job (not one job per task), in-memory job store: there's
nothing to persist beyond what's already durable in Postgres (due_at /
last_reminded_at), so the schedule simply re-registers itself on each boot.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.config import settings
from db.session import async_session
import services.tasks.task_service as task_service

logger = logging.getLogger("scheduler")

# Single-user placeholder until auth/profiles arrive (Milestone 5); consistent
# with the "local-user" default used across the websocket + REST layers.
_USER_ID = "local-user"

_scheduler: AsyncIOScheduler | None = None


async def check_due_tasks() -> None:
    """Read-only sweep: log anything currently due. Never mutates state."""
    async with async_session() as session:
        due = await task_service.get_due_reminders(session, _USER_ID)
    if due:
        logger.info(
            "Sweep: %d due reminder(s) pending delivery — %s",
            len(due),
            "; ".join(t.title for t in due),
        )


def start_scheduler() -> AsyncIOScheduler:
    """Create + start the recurring sweep. Call once from the app lifespan."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        check_due_tasks,
        trigger="interval",
        seconds=settings.REMINDER_SWEEP_SECONDS,
        id="check_due_tasks",
        replace_existing=True,
        coalesce=True,           # collapse missed runs into one
        max_instances=1,         # never overlap sweeps
    )
    _scheduler.start()
    logger.info(
        "Reminder sweep started — every %ds", settings.REMINDER_SWEEP_SECONDS
    )
    return _scheduler


def shutdown_scheduler() -> None:
    """Stop the scheduler on app shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Reminder sweep stopped")
