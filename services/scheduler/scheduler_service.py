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
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.config import settings
from db.session import async_session
import services.tasks.task_service as task_service
import services.memory.profile_service as profile_service
import services.reminders.reminder_service as reminder_service
import services.devices.device_service as device_service
import services.engagement.checkin_service as checkin_service
from services.push import push_service
from models.task import Task
from models.reminder import Reminder
from utils import timez

logger = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _all_user_ids() -> list[str]:
    async with async_session() as session:
        return await task_service.list_user_ids(session)


async def check_due_tasks() -> None:
    """Read-only sweep: log anything currently due, for every user. Never mutates."""
    for user_id in await _all_user_ids():
        async with async_session() as session:
            due = await task_service.get_due_reminders(session, user_id)
        if due:
            logger.info(
                "Sweep: %d due reminder(s) pending delivery for %s — %s",
                len(due),
                user_id,
                "; ".join(t.title for t in due),
            )


# ════════════════════════════════════════════════════════════════════════
#  Push delivery sweep (the outbound half — claims the ledger and sends FCM)
# ════════════════════════════════════════════════════════════════════════
def _build_copy(task: Task, reminder: Reminder) -> tuple[str, str, dict]:
    """Build a clean notification title/body (no emoji, times in IST) plus the
    data payload the mobile client uses for tap-routing and the delivery ack.

    The title is just the task name; the body carries the timing. The mobile
    client adds a small "Reminder" sub-label, so the copy itself stays plain.
    """
    when = timez.fmt_clock(task.due_at)
    title = task.title
    if reminder.offset_minutes <= 0:
        body = f"Happening now ({when})" if when else "It's time."
    else:
        mins = reminder.offset_minutes
        if mins < 60:
            lead = f"{mins} minute{'s' if mins != 1 else ''}"
        else:
            hrs = mins // 60
            lead = f"{hrs} hour{'s' if hrs != 1 else ''}"
        body = f"Starts in {lead}" + (f" ({when})" if when else "")

    data = {
        "type": "task_reminder",
        "task_id": task.id,
        "reminder_id": reminder.id,
        "offset_minutes": reminder.offset_minutes,
        "task_title": task.title,
        "due_at": task.due_at.isoformat() if task.due_at else "",
    }
    return title, body, data


async def _deliver_one(reminder_id: str) -> None:
    """Send a single claimed reminder and finalise its ledger row.

    Three phases, each in its own short transaction — the slow network send
    happens OUTSIDE any DB lock so claimed rows aren't held open during I/O.
    """
    # Phase 1 — gather (the task + the user's tokens + the copy).
    async with async_session() as session:
        reminder = await session.get(Reminder, reminder_id)
        if reminder is None or reminder.status != "claimed":
            return  # already finalised or reclaimed elsewhere
        task = await session.get(Task, reminder.task_id)
        if task is None or task.status in ("done", "cancelled"):
            # Task finished/cancelled between claim and send → don't notify.
            reminder.status = "cancelled"
            await session.commit()
            return
        tokens = await device_service.tokens_for(session, reminder.user_id)
        title, body, data = _build_copy(task, reminder)

    if not tokens:
        # Nothing to deliver to yet — hold (no attempt burned) until a device
        # registers or the claim times out and it's retried.
        async with async_session() as session:
            await reminder_service.hold(session, reminder_id, "no_registered_device")
            await session.commit()
        return

    # Phase 2 — send (no transaction held).
    result = await push_service.send_reminder(tokens, title=title, body=body, data=data)

    # Phase 3 — finalise based on the outcome.
    async with async_session() as session:
        for dead in result.prune_tokens:
            await device_service.prune(session, dead)
        if result.delivered:
            await reminder_service.mark_sent(session, reminder_id)
        elif result.transient:
            await reminder_service.mark_retry(session, reminder_id, result.error)
        else:
            # No delivery and not a transient error → every token was invalid and
            # has now been pruned; hold for a future device rather than burn it.
            await reminder_service.hold(session, reminder_id, result.error or "no_valid_tokens")
        await session.commit()


async def deliver_due_reminders() -> None:
    """Claim every due reminder and push it. Safe no-op until FCM is configured,
    so the rest of the app runs unchanged without credentials."""
    if not push_service.is_configured():
        return
    async with async_session() as session:
        claimed = await reminder_service.claim_due(session)
        ids = [r.id for r in claimed]
        await session.commit()
    if not ids:
        return
    logger.info("Push delivery: claimed %d due reminder(s)", len(ids))
    for reminder_id in ids:
        try:
            await _deliver_one(reminder_id)
        except Exception as exc:  # noqa: BLE001 — one bad reminder must not stop the rest
            logger.warning("reminder delivery error (%s): %s", reminder_id, exc)


def _local_now(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(ZoneInfo("UTC"))


async def _checkin_one_user(user_id: str) -> None:
    """
    Per-user daily check-in push — this runs hourly for every user, but each user
    only actually receives a check-in once a day at THEIR configured local
    check-in hour (the same hour that drives the research refresh).

    Composes a time-of-day-aware summary (today vs tomorrow + overdue) and pushes
    it via FCM. Marked once-per-local-day via `last_checkin_on`. Skips silently
    when there's nothing to say, no device is registered, or FCM isn't configured.
    """
    async with async_session() as session:
        profile = await profile_service.ensure_profile(session, user_id)
        hour = profile_service.checkin_hour(profile)
        local = _local_now(profile.timezone)
        already = profile.last_checkin_on == local.date()
        await session.commit()

    if local.hour != hour or already:
        return

    # Build the message + gather device tokens.
    async with async_session() as session:
        profile = await profile_service.ensure_profile(session, user_id)
        content = await checkin_service.build_checkin(session, user_id, profile)
        tokens = await device_service.tokens_for(session, user_id)
        await session.commit()

    if content and tokens and push_service.is_configured():
        title, body = content
        result = await push_service.send_reminder(
            tokens, title=title, body=body, data={"type": "daily_checkin"}
        )
        if result.prune_tokens:
            async with async_session() as session:
                for dead in result.prune_tokens:
                    await device_service.prune(session, dead)
                await session.commit()
        logger.info("Daily check-in delivered=%d for %s", result.success_count, user_id)

    # Mark processed for the day regardless of whether we sent, so we evaluate the
    # check-in exactly once per local day.
    async with async_session() as session:
        await profile_service.mark_checked_in(session, user_id, local.date())
        await session.commit()


async def daily_checkin_sweep() -> None:
    """Fan out the per-user daily check-in across every known user."""
    for user_id in await _all_user_ids():
        try:
            await _checkin_one_user(user_id)
        except Exception as exc:  # noqa: BLE001 — one user's failure mustn't stop the rest
            logger.warning("Daily check-in failed for %s: %s", user_id, exc)


def start_scheduler() -> AsyncIOScheduler:
    """Create + start the recurring sweep. Call once from the app lifespan."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler()

    # Cheap, frequent due-task detection (read-only).
    _scheduler.add_job(
        check_due_tasks,
        trigger="interval",
        seconds=settings.REMINDER_SWEEP_SECONDS,
        id="check_due_tasks",
        replace_existing=True,
        coalesce=True,           # collapse missed runs into one
        max_instances=1,         # never overlap sweeps
    )

    # Outbound push delivery — claims the reminder ledger and sends FCM. Self-gates
    # to a no-op until FCM credentials are configured.
    _scheduler.add_job(
        deliver_due_reminders,
        trigger="interval",
        seconds=settings.REMINDER_DELIVERY_SECONDS,
        id="deliver_due_reminders",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Daily check-in push — fires hourly, self-gates to each user's local
    # check-in hour, sends a time-of-day-aware summary (restart-safe via
    # last_checkin_on). No-op until FCM is configured.
    _scheduler.add_job(
        daily_checkin_sweep,
        trigger="cron",
        minute=0,
        id="daily_checkin_sweep",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started — due-sweep %ds, daily refresh @local-hour, reflections @07:00 UTC",
        settings.REMINDER_SWEEP_SECONDS,
    )
    return _scheduler


def get_job_status() -> list[dict]:
    """Return a snapshot of every APScheduler job — id, next run time, trigger.

    Purely read-only introspection; the scheduler's actual behaviour is
    unaffected.  Returns an empty list when the scheduler hasn't started.
    """
    if _scheduler is None:
        return []
    jobs = _scheduler.get_jobs()
    out = []
    for job in jobs:
        out.append({
            "id": job.id,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return out


def shutdown_scheduler() -> None:
    """Stop the scheduler on app shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Reminder sweep stopped")
