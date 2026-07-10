"""
Task tool implementations — Postgres-backed (Milestone 2).

Thin adapters: parse LLM-supplied args, open a DB session, call into
`services.task_service`, commit, and return the same dict contract the
tool-calling layer already expects (with a `summary` for the LLM to speak).
"""

import logging
from datetime import datetime

from db.session import async_session
from models.task import Task
import services.tasks.task_service as svc
from utils import timez

logger = logging.getLogger(__name__)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.warning("Could not parse datetime: %r", value)
        return None
    # A model may emit a clock time with no timezone ("2026-06-29T20:00:00") —
    # interpret it as LOCAL time (the configured default, e.g. IST), not UTC, so
    # "8pm" means 8pm where the user is. Stored tz-aware for consistent compares.
    return timez.ensure_aware(dt)


def _parse_int(value) -> int | None:
    """Coerce a model-supplied value to a positive int, or None if unusable."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _parse_offsets(value) -> list[int] | None:
    """Sanitize the model-supplied reminder offsets (minutes before due). Returns
    None when nothing usable was given, so the service applies its default."""
    if not isinstance(value, (list, tuple)):
        return None
    out: list[int] = []
    for v in value:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            out.append(n)
    return out or None


def _user(session_context: dict) -> str:
    return session_context["user_id"]


async def create_task(args: dict, session_context: dict) -> dict:
    user_id = _user(session_context)

    # Structural confirmation gate: never create a task the user didn't explicitly
    # ask for. The model must assert `user_confirmed=true`; if it's only inferring
    # the task would help, nothing is written and it's told to ask the user first.
    if not args.get("user_confirmed", False):
        return {
            "ok": False,
            "needs_confirmation": True,
            "summary": "Not created — ask the user to confirm they want this task first.",
        }

    # Stash research findings (from a prior `research` tool call this turn) onto
    # the task's freeform context JSONB, so the task carries its sources.
    context = None
    research_summary = args.get("research_summary")
    source_links = args.get("source_links")
    research_query = args.get("research_query")
    success_condition = args.get("success_condition")
    retry_interval = args.get("retry_interval_days")

    if research_summary or source_links:
        context = {"research": {"summary": research_summary, "links": source_links or []}}

    # When the task needs background research polling, store the structured
    # intent so the scheduler knows exactly what to search for and when to stop.
    if research_query or success_condition:
        context = context or {}
        context["research_intent"] = {
            "query": research_query or "",
            "success_condition": success_condition or "",
            "retry_interval_days": retry_interval or 7,
        }

    async with async_session() as session:
        task, parent = await svc.create_task(
            session,
            user_id,
            title=args.get("title", "Untitled task"),
            description=args.get("description"),
            parent=args.get("parent_task"),
            depends_on=args.get("depends_on_task"),
            due_at=_parse_dt(args.get("due_at")),
            window_start=_parse_dt(args.get("window_start")),
            window_end=_parse_dt(args.get("window_end")),
            needs_research=bool(args.get("needs_research", False)),
            context=context,
            reminder_offsets=_parse_offsets(args.get("reminder_offsets_minutes")),
            auto_archive_after_hours=_parse_int(args.get("auto_archive_after_hours")),
        )
        await session.commit()
        nested = f" (nested under '{parent.title}')" if parent else ""
        blocked = " It's blocked until its prerequisite is done." if task.status == "blocked" else ""
        return {
            "ok": True,
            "task": task.to_brief(),
            "summary": f"Created task '{task.title}'{nested}.{blocked}",
        }


async def update_task(args: dict, session_context: dict) -> dict:
    user_id = _user(session_context)
    task_ref = args.get("task", "")
    if not task_ref:
        return {"ok": False, "error": "missing_task", "summary": "Which task should I update?"}

    async with async_session() as session:
        task = await svc.update_task(
            session,
            user_id,
            task_ref,
            title=args.get("title"),
            description=args.get("description"),
            due_at=_parse_dt(args.get("due_at")),
            window_start=_parse_dt(args.get("window_start")),
            window_end=_parse_dt(args.get("window_end")),
            parent=args.get("parent_task"),
            depends_on=args.get("depends_on_task"),
            research_summary=args.get("research_summary"),
            source_links=args.get("source_links"),
            reminder_offsets=_parse_offsets(args.get("reminder_offsets_minutes")),
        )
        if task is None:
            return {"ok": False, "error": "task_not_found", "summary": "I couldn't find that task."}
        await session.commit()
        return {"ok": True, "task": task.to_brief(), "summary": f"Updated '{task.title}'."}


async def query_tasks(args: dict, session_context: dict) -> dict:
    user_id = _user(session_context)
    async with async_session() as session:
        tasks = await svc.get_tasks(
            session,
            user_id,
            scope=args.get("scope", "all_active"),
            status_filter=args.get("status_filter"),
            search_text=args.get("search_text"),
            due_after=_parse_dt(args.get("due_after")),
            due_before=_parse_dt(args.get("due_before")),
        )
        brief = []
        for t in tasks:
            b = t.to_brief()
            if t.parent_id:
                parent = await session.get(Task, t.parent_id)
                b["parent_title"] = parent.title if parent else None
            brief.append(b)

    if brief:
        titles = ", ".join(t["title"] for t in brief)
        summary = f"Found {len(brief)} task(s): {titles}."
    else:
        summary = "No matching tasks found."
    return {"ok": True, "tasks": brief, "summary": summary}


async def update_task_status(args: dict, session_context: dict) -> dict:
    user_id = _user(session_context)
    new_status = args.get("new_status", "")
    if new_status not in svc.VALID_STATUSES:
        return {"ok": False, "error": "invalid_status", "summary": f"'{new_status}' is not a valid status."}

    async with async_session() as session:
        task, unblocked = await svc.update_status(
            session, user_id, args.get("task", ""), new_status, args.get("note")
        )
        if task is None:
            return {"ok": False, "error": "task_not_found", "summary": "I couldn't find that task."}
        result = {"id": task.id, "title": task.title, "status": task.status}
        await session.commit()

    extra = f" Unblocked: {', '.join(unblocked)}." if unblocked else ""
    return {"ok": True, "task": result, "summary": f"Marked '{result['title']}' as {new_status}.{extra}"}
