"""
Task service — business logic over the `tasks` table.

The tool layer (`services/tools/task_tools.py`) is a thin adapter that opens a
session and calls into here, so this logic stays independently testable.
"""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User
from models.task import Task

logger = logging.getLogger(__name__)

VALID_STATUSES = {"pending", "blocked", "active", "done", "cancelled"}
_CLOSED = ("done", "cancelled")


async def list_user_ids(session: AsyncSession) -> list[str]:
    """All known user ids — drives the scheduler's per-user job loops."""
    rows = (await session.execute(select(User.id))).scalars().all()
    return list(rows)


async def ensure_user(
    session: AsyncSession,
    user_id: str,
    display_name: str | None = None,
    email: str | None = None,
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, display_name=display_name, email=email)
        session.add(user)
        await session.flush()
    return user


_MATCH_STOPWORDS = {
    "a", "an", "the", "to", "for", "of", "my", "me", "i", "on", "in", "at",
    "set", "up", "reminder", "task", "remind", "about", "and", "is", "was",
}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())}


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC so comparisons never crash on a
    naive value (Postgres returns tz-aware, but a tool-supplied ISO date may be
    naive)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def find_task(session: AsyncSession, user_id: str, needle: str | None) -> Task | None:
    """Resolve a task by exact id first, then title match. Tries substring,
    then word-overlap (so 'book a train to Cairo' still finds 'Cairo train
    tickets'), preferring tasks that aren't already done/cancelled."""
    needle = (needle or "").strip()
    if not needle:
        return None

    exact = await session.get(Task, needle)
    if exact is not None and exact.user_id == user_id:
        return exact

    rows = (await session.execute(select(Task).where(Task.user_id == user_id))).scalars().all()
    if not rows:
        return None

    # 1) substring match (precise)
    nl = needle.lower()
    matches = [t for t in rows if nl in t.title.lower()]

    # 2) fall back to word-overlap ranking
    if not matches:
        query_words = _words(nl) - _MATCH_STOPWORDS
        query_words = {w for w in query_words if len(w) > 1}
        if query_words:
            scored = [
                (len(query_words & _words(t.title)), t) for t in rows
            ]
            scored = [(n, t) for n, t in scored if n > 0]
            # Most-overlapping first; the stable sort below keeps that order
            # within the open/closed grouping.
            scored.sort(key=lambda x: -x[0])
            matches = [t for _, t in scored]

    if not matches:
        return None
    matches.sort(key=lambda t: t.status in _CLOSED)  # open tasks first
    return matches[0]


async def find_relevant_tasks(
    session: AsyncSession, user_id: str, text: str, limit: int = 3
) -> list[Task]:
    """
    Deterministically scan free text (the user's raw utterance) for ACTIVE
    tasks it might be referring to, by title word-overlap — same scoring
    `find_task` uses, just applied against a whole sentence instead of a
    single named reference.

    This exists so the pipeline can proactively surface a task's stored
    context (research links, notes) BEFORE the model decides which tool to
    call — removing reliance on model judgment for "is this about something
    I already tracked," which is unreliable by nature. False positives here
    are cheap (one extra ignored hint); false negatives are the actual bug
    class this fixes, so the match is intentionally permissive (>=1
    significant overlapping word).
    """
    words = _words(text) - _MATCH_STOPWORDS
    words = {w for w in words if len(w) > 1}
    if not words:
        return []

    rows = (
        await session.execute(select(Task).where(Task.user_id == user_id))
    ).scalars().all()
    active = [t for t in rows if t.status not in _CLOSED]
    if not active:
        return []

    scored = [(len(words & _words(t.title)), t) for t in active]
    scored = [(n, t) for n, t in scored if n > 0]
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:limit]]


async def create_task(
    session: AsyncSession,
    user_id: str,
    *,
    title: str,
    description: str | None = None,
    parent: str | None = None,
    depends_on: str | None = None,
    due_at: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    needs_research: bool = False,
    context: dict | None = None,
) -> tuple[Task, Task | None]:
    await ensure_user(session, user_id)

    parent_task = await find_task(session, user_id, parent) if parent else None
    dep_task = await find_task(session, user_id, depends_on) if depends_on else None

    blocked = dep_task is not None and dep_task.status not in _CLOSED

    task = Task(
        user_id=user_id,
        title=title,
        description=description,
        parent_id=parent_task.id if parent_task else None,
        depends_on_id=dep_task.id if dep_task else None,
        status="blocked" if blocked else "pending",
        due_at=due_at,
        window_start=window_start,
        window_end=window_end,
        requires_research=needs_research,
        context=context,
    )
    session.add(task)

    # A task that now has a child is a milestone (grouping) node.
    if parent_task is not None and parent_task.task_type != "milestone":
        parent_task.task_type = "milestone"

    await session.flush()
    logger.info("create_task → %s (%s)", task.title, task.id)
    return task, parent_task


async def update_task(
    session: AsyncSession,
    user_id: str,
    task_ref: str,
    *,
    title: str | None = None,
    description: str | None = None,
    due_at: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    parent: str | None = None,
    depends_on: str | None = None,
    research_summary: str | None = None,
    source_links: list | None = None,
) -> Task | None:
    """
    Patch fields on an EXISTING task — distinct from update_status (status-only).
    This is what 'add the link/fee to that reminder' should call instead of
    create_task, so amending a task doesn't produce a duplicate row.

    Research findings are MERGED into context.research, not overwritten — calling
    this twice (e.g. once for the link, later for a date change) doesn't clobber
    what was already stored.
    """
    task = await find_task(session, user_id, task_ref)
    if task is None:
        return None

    if title:
        task.title = title
    if description:
        task.description = description
    if due_at is not None:
        task.due_at = due_at
    if window_start is not None:
        task.window_start = window_start
    if window_end is not None:
        task.window_end = window_end

    if parent:
        parent_task = await find_task(session, user_id, parent)
        if parent_task is not None:
            task.parent_id = parent_task.id
            if parent_task.task_type != "milestone":
                parent_task.task_type = "milestone"

    if depends_on:
        dep_task = await find_task(session, user_id, depends_on)
        if dep_task is not None:
            task.depends_on_id = dep_task.id
            if dep_task.status not in _CLOSED and task.status not in _CLOSED:
                task.status = "blocked"

    if research_summary or source_links:
        ctx = dict(task.context or {})
        existing = dict(ctx.get("research") or {})
        if research_summary:
            existing["summary"] = research_summary
        if source_links:
            existing["links"] = source_links
        ctx["research"] = existing
        task.context = ctx

    await session.flush()
    logger.info("update_task → %s (%s)", task.title, task.id)
    return task


async def get_tasks(
    session: AsyncSession,
    user_id: str,
    scope: str = "all_active",
    status_filter: str | None = None,
    search_text: str | None = None,
    due_after: datetime | None = None,
    due_before: datetime | None = None,
) -> list[Task]:
    rows = (
        await session.execute(
            select(Task).where(Task.user_id == user_id).order_by(Task.created_at)
        )
    ).scalars().all()

    if scope == "all":
        return list(rows)
    if scope == "by_status" and status_filter:
        return [t for t in rows if t.status == status_filter]
    if scope == "specific_task":
        found = await find_task(session, user_id, search_text)
        return [found] if found else []

    active = [t for t in rows if t.status not in _CLOSED]
    now = datetime.now(timezone.utc)

    # Explicit date-range query (e.g. "next month", "in December"). Only dated
    # tasks qualify — a date-range question is about scheduled tasks.
    if due_after is not None or due_before is not None:
        lo, hi = _aware(due_after), _aware(due_before)

        def in_range(t: Task) -> bool:
            d = _aware(t.due_at)
            if d is None:
                return False
            if lo is not None and d < lo:
                return False
            if hi is not None and d > hi:
                return False
            return True

        return [t for t in active if in_range(t)]

    if scope == "overdue":
        return [t for t in active if (d := _aware(t.due_at)) is not None and d < now]

    if scope in ("today", "this_week", "this_month"):
        horizon_days = {"today": 1, "this_week": 7, "this_month": 31}[scope]

        def due_within(t: Task) -> bool:
            d = _aware(t.due_at)
            if d is None:
                return True  # undated active tasks always show
            return (d - now).total_seconds() <= horizon_days * 86400

        return [t for t in active if due_within(t)]

    # default: all_active
    return active


async def update_status(
    session: AsyncSession,
    user_id: str,
    task_ref: str,
    new_status: str,
    note: str | None = None,
) -> tuple[Task | None, list[str]]:
    task = await find_task(session, user_id, task_ref)
    if task is None:
        return None, []

    task.status = new_status
    if note:
        ctx = dict(task.context or {})
        ctx.setdefault("notes", []).append(note)
        task.context = ctx
    await session.flush()

    # When a task completes, unblock anything that was waiting on it.
    unblocked: list[str] = []
    if new_status == "done":
        deps = (
            await session.execute(
                select(Task).where(
                    Task.user_id == user_id,
                    Task.depends_on_id == task.id,
                    Task.status == "blocked",
                )
            )
        ).scalars().all()
        for d in deps:
            d.status = "pending"
            unblocked.append(d.title)
        await session.flush()

    logger.info("update_status → %s = %s (unblocked %d)", task.title, new_status, len(unblocked))
    return task, unblocked


async def get_due_reminders(session: AsyncSession, user_id: str) -> list[Task]:
    """Open tasks that are due (or overdue) and haven't been announced yet."""
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status.in_(("pending", "active")),
                Task.due_at.is_not(None),
                Task.due_at <= now,
                Task.last_reminded_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def mark_reminded(session: AsyncSession, tasks: list[Task]) -> None:
    """Stamp tasks as announced so a later sweep/connect doesn't repeat them."""
    now = datetime.now(timezone.utc)
    for t in tasks:
        t.last_reminded_at = now
    await session.flush()


def _reminder_message(tasks: list[Task]) -> str:
    """Build the spoken-style summary for a set of due tasks (singular vs plural)."""
    if len(tasks) == 1:
        return f"Quick reminder — '{tasks[0].title}' is due."
    titles = "; ".join(t.title for t in tasks)
    return f"Quick reminder — you have {len(tasks)} things due: {titles}."


async def consume_due_reminders(
    session: AsyncSession, user_id: str
) -> tuple[list[Task], str]:
    """
    Fetch due-and-unreminded tasks AND mark them reminded in one transaction.

    Calling this *is* the act of delivering the reminder — whoever decides "now
    is a necessary time" (a session-start hook, the mobile app, a manual test)
    calls it via the REST endpoint. The scheduler never calls this; it only
    detects (read-only). Keeping the "mark reminded" side-effect exclusively
    here avoids two halves racing to swallow the same reminder.

    Returns (tasks, spoken_message). Caller is responsible for committing.
    """
    due = await get_due_reminders(session, user_id)
    if not due:
        return [], ""
    await mark_reminded(session, due)
    return due, _reminder_message(due)


async def get_research_schedule(
    session: AsyncSession, user_id: str
) -> list[dict]:
    """Every task with `requires_research=True`, with its structured research
    intent and last refresh outcome surfaced for the debug panel.

    Sorted by `next_attempt_at` soonest-first; legacy tasks without a
    structured intent sort last (they poll on every sweep).
    """
    rows = (
        await session.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.requires_research.is_(True),
            )
        )
    ).scalars().all()

    schedule = []
    for t in rows:
        ctx = t.context or {}
        intent = ctx.get("research_intent") or {}
        refresh = ctx.get("research_refresh") or {}
        schedule.append({
            "task_id": t.id,
            "title": t.title,
            "status": t.status,
            "query": intent.get("query", ""),
            "success_condition": intent.get("success_condition", ""),
            "retry_interval_days": intent.get("retry_interval_days"),
            "next_attempt_at": intent.get("next_attempt_at"),
            "last_outcome": refresh.get("note") or refresh.get("summary") or None,
        })

    # Soonest retry first; None sorts last.
    schedule.sort(key=lambda s: s["next_attempt_at"] or "9999")
    return schedule
