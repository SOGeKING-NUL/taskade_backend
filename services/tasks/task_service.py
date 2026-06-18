"""
Task service — business logic over the `tasks` table.

The tool layer (`services/tools/task_tools.py`) is a thin adapter that opens a
session and calls into here, so this logic stays independently testable.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User
from models.task import Task

logger = logging.getLogger(__name__)

VALID_STATUSES = {"pending", "blocked", "active", "done", "cancelled"}
_CLOSED = ("done", "cancelled")


async def ensure_user(session: AsyncSession, user_id: str, display_name: str | None = None) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, display_name=display_name)
        session.add(user)
        await session.flush()
    return user


async def find_task(session: AsyncSession, user_id: str, needle: str | None) -> Task | None:
    """Resolve a task by exact id first, then case-insensitive title match
    (preferring tasks that aren't already done/cancelled)."""
    needle = (needle or "").strip()
    if not needle:
        return None

    exact = await session.get(Task, needle)
    if exact is not None and exact.user_id == user_id:
        return exact

    rows = (await session.execute(select(Task).where(Task.user_id == user_id))).scalars().all()
    nl = needle.lower()
    matches = [t for t in rows if nl in t.title.lower()]
    if not matches:
        return None
    matches.sort(key=lambda t: t.status in _CLOSED)  # open tasks first
    return matches[0]


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


async def get_tasks(
    session: AsyncSession,
    user_id: str,
    scope: str = "all_active",
    status_filter: str | None = None,
    search_text: str | None = None,
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
    if scope in ("today", "this_week", "this_month"):
        horizon_days = {"today": 1, "this_week": 7, "this_month": 31}[scope]
        now = datetime.now(timezone.utc)

        def due_within(t: Task) -> bool:
            if t.due_at is None:
                return True  # undated active tasks always show
            return (t.due_at - now).total_seconds() <= horizon_days * 86400

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
