"""
Reminder ledger service — generates reminder rows from a task and drives the
zero-loss / zero-dup delivery state machine.

Two responsibilities:

1. SYNC (called from the task layer): translate a task's `due_at` + a set of
   "minutes before due" offsets into `reminders` rows, in place — so changing a
   task's due date re-arms its reminders and completing/cancelling it stands them
   down, without ever duplicating a row.

2. DELIVERY (called from the scheduler sweep): atomically CLAIM due reminders
   and transition them to `sent` / `pending` (retry) / `failed`, plus the mobile
   `delivered` acknowledgement.

All functions take a session and leave the commit to the caller (mirroring
`task_service`), except where noted.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.task import Task
from models.reminder import Reminder, PENDING, CLAIMED, SENT, FAILED, CANCELLED
from utils import timez

logger = logging.getLogger(__name__)

# Non-terminal statuses — rows that a re-sync may re-arm or stand down.
_LIVE = (PENDING, CLAIMED)


def default_offsets() -> list[int]:
    """Configured default offsets (minutes before due) — e.g. [0, 10]."""
    return _normalize_offsets(
        [p.strip() for p in settings.REMINDER_DEFAULT_OFFSETS.split(",")]
    )


def _normalize_offsets(raw) -> list[int]:
    """Sanitize an offsets list: non-negative ints, de-duplicated, sorted.
    Falls back to the configured default when nothing usable is supplied."""
    out: set[int] = set()
    for v in raw or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            out.add(n)
    if not out:
        # Avoid recursion: read the raw default directly here.
        for p in settings.REMINDER_DEFAULT_OFFSETS.split(","):
            try:
                n = int(p.strip())
            except ValueError:
                continue
            if n >= 0:
                out.add(n)
    return sorted(out)


# Escalating "remind me until it starts" ladder (minutes before due), densest
# near the deadline. We pick the entries that still fall in the future and cap
# the count — so a request produces a useful ramp-up, never a spammy stream.
_RAMP_LADDER = [0, 10, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720, 1440]


def ramp_up_offsets(
    due_at: datetime, now: datetime | None = None, max_count: int | None = None
) -> list[int]:
    """Build an escalating set of reminder offsets leading up to `due_at`.

    Returns "minutes before due" values that get more frequent near the time
    (e.g. for a task ~2h out: at-time, 10, 20, 30, 45, 60, 90, 120 min before),
    keeping only those that fire in the FUTURE and capping the total at
    `REMINDER_MAX_RAMP`. Nearest-to-deadline entries win under the cap, so the
    reminders cluster where they matter most.
    """
    if max_count is None:
        max_count = settings.REMINDER_MAX_RAMP
    due = timez.ensure_aware(due_at)
    now = now or datetime.now(timezone.utc)
    if due is None:
        return [0]
    minutes_until = (due - now).total_seconds() / 60.0

    # offset 0 (at-time) is always eligible; others only if they land before now.
    candidates = [o for o in _RAMP_LADDER if o == 0 or o < minutes_until]
    candidates = sorted(set(candidates))  # ascending = nearest the deadline first
    if len(candidates) <= max_count:
        return candidates
    # Over the cap: keep the densest cluster near the deadline, but reserve the
    # last slot for the FARTHEST-out reminder — so a far event still gets an early
    # heads-up instead of total silence until a couple of hours before.
    near = candidates[: max_count - 1]
    far = candidates[-1]
    return sorted(set(near + [far]))


def _label_for(offset_minutes: int) -> str:
    if offset_minutes <= 0:
        return "now"
    if offset_minutes % 60 == 0:
        hrs = offset_minutes // 60
        return f"{hrs} hour{'s' if hrs != 1 else ''} before"
    return f"{offset_minutes} minute{'s' if offset_minutes != 1 else ''} before"


# ════════════════════════════════════════════════════════════════════════
#  SYNC — task → reminder rows
# ════════════════════════════════════════════════════════════════════════
async def sync_for_task(
    session: AsyncSession,
    task: Task,
    offsets: list[int] | None = None,
) -> None:
    """Reconcile a task's reminder rows with the desired set of offsets.

    Rules (all `fire_at` comparisons are UTC, so timezone-correct by construction):
      • No `due_at` → stand down every live reminder; create none.
      • For each offset, `fire_at = due_at - offset`. Only reminders strictly in
        the FUTURE are kept — a lead reminder whose moment already passed (task
        created <N min before the event) is skipped, and an at-time reminder for
        a past due date is skipped (overdue surfacing is the pull path's job).
      • Existing rows are UPDATED in place (preserving a row already `sent` when
        its `fire_at` is unchanged, so an unrelated edit never re-fires it); only
        a genuinely new/changed `fire_at` re-arms a row to `pending`.
      • Offsets no longer desired are cancelled (live rows only).
    """
    now = datetime.now(timezone.utc)
    due = timez.ensure_aware(task.due_at)

    existing = (
        await session.execute(select(Reminder).where(Reminder.task_id == task.id))
    ).scalars().all()
    by_offset: dict[int, Reminder] = {r.offset_minutes: r for r in existing}

    # Desired offset → fire_at (future only).
    desired: dict[int, datetime] = {}
    if due is not None:
        for off in _normalize_offsets(offsets):
            fire_at = due - timedelta(minutes=off)
            if fire_at > now:
                desired[off] = fire_at

    # Update / create the desired rows.
    for off, fire_at in desired.items():
        row = by_offset.get(off)
        if row is None:
            session.add(
                Reminder(
                    task_id=task.id,
                    user_id=task.user_id,
                    fire_at=fire_at,
                    offset_minutes=off,
                    label=_label_for(off),
                    status=PENDING,
                )
            )
            continue
        # Re-arm ONLY when the moment actually changed — otherwise leave the row
        # (and any `sent`/`delivered` state) exactly as it is.
        if timez.ensure_aware(row.fire_at) != fire_at:
            row.fire_at = fire_at
            row.label = _label_for(off)
            row.status = PENDING
            row.attempts = 0
            row.claimed_at = None
            row.sent_at = None
            row.delivered_at = None
            row.last_error = None

    # Stand down live rows whose offset is no longer wanted.
    for off, row in by_offset.items():
        if off not in desired and row.status in _LIVE:
            row.status = CANCELLED

    await session.flush()


async def cancel_for_task(session: AsyncSession, task_id: str) -> int:
    """Cancel a task's still-live reminders (on done/cancelled). Leaves rows that
    already fired (`sent`) as a delivery record. Returns rows cancelled."""
    rows = (
        await session.execute(
            select(Reminder).where(
                Reminder.task_id == task_id, Reminder.status.in_(_LIVE)
            )
        )
    ).scalars().all()
    for r in rows:
        r.status = CANCELLED
    await session.flush()
    return len(rows)


# ════════════════════════════════════════════════════════════════════════
#  DELIVERY — claim + transition
# ════════════════════════════════════════════════════════════════════════
async def claim_due(session: AsyncSession, limit: int | None = None) -> list[Reminder]:
    """Atomically claim up to `limit` reminders that are ready to fire.

    Zero-dup core: `SELECT … FOR UPDATE SKIP LOCKED` means concurrent sweeps (or
    a future multi-worker deploy) never claim the same row. Picks up both fresh
    `pending` rows and `claimed` rows whose send crashed (claim aged past the
    timeout). Marks them `claimed` and returns them; the caller commits, then
    sends OUTSIDE the lock and finalises each via `mark_*`.

    `attempts` is intentionally NOT bumped here — a claim is not a send attempt;
    only a real transient send failure (`mark_retry`) counts toward the cap.
    """
    if limit is None:
        limit = settings.REMINDER_BATCH_LIMIT
    now = datetime.now(timezone.utc)
    stuck_cutoff = now - timedelta(seconds=settings.REMINDER_CLAIM_TIMEOUT_SECONDS)

    stmt = (
        select(Reminder)
        .where(
            Reminder.fire_at <= now,
            or_(
                Reminder.status == PENDING,
                and_(Reminder.status == CLAIMED, Reminder.claimed_at < stuck_cutoff),
            ),
        )
        .order_by(Reminder.fire_at)
        .limit(limit)
        # skip_locked is honored on Postgres; harmlessly ignored on dialects
        # without row locking (e.g. sqlite in tests).
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    for r in rows:
        r.status = CLAIMED
        r.claimed_at = now
    await session.flush()
    return list(rows)


async def mark_sent(session: AsyncSession, reminder_id: str) -> None:
    r = await session.get(Reminder, reminder_id)
    if r is None:
        return
    r.status = SENT
    r.sent_at = datetime.now(timezone.utc)
    r.last_error = None
    await session.flush()


async def mark_retry(session: AsyncSession, reminder_id: str, error: str) -> None:
    """Return a reminder to `pending` after a transient failure, or `failed` once
    attempts hit the cap (logged, never silently dropped)."""
    r = await session.get(Reminder, reminder_id)
    if r is None:
        return
    r.attempts += 1
    r.last_error = (error or "")[:500]
    if r.attempts >= settings.REMINDER_MAX_ATTEMPTS:
        r.status = FAILED
        logger.error(
            "reminder %s FAILED after %d attempts: %s", r.id, r.attempts, r.last_error
        )
    else:
        r.status = PENDING
    await session.flush()


async def hold(session: AsyncSession, reminder_id: str, reason: str) -> None:
    """Return a reminder to `pending` WITHOUT counting an attempt — for conditions
    that aren't the device's fault (no token registered yet, all tokens just
    pruned). It simply waits for the next sweep / a device to appear."""
    r = await session.get(Reminder, reminder_id)
    if r is None:
        return
    r.status = PENDING
    r.last_error = (reason or "")[:500]
    await session.flush()


async def mark_delivered(session: AsyncSession, user_id: str, reminder_id: str) -> bool:
    """Record the mobile client's acknowledgement that the notification actually
    surfaced. Scoped to the owner so one user can't ack another's reminder."""
    r = await session.get(Reminder, reminder_id)
    if r is None or r.user_id != user_id:
        return False
    if r.delivered_at is None:
        r.delivered_at = datetime.now(timezone.utc)
    await session.flush()
    return True
