"""
Task tool implementations.

Milestone 1: an in-memory stub store so the tool-calling pipeline can be wired
and demoed end-to-end. Milestone 2 swaps these bodies for real Postgres-backed
calls via `services/task_service.py` — the function signatures and return
contracts stay the same so nothing upstream changes.

Each tool takes (args: dict, session_context: dict) and returns a JSON-able dict.
A `summary` key is included for the LLM to read back to the user naturally.
"""

import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# user_id -> list[task dict]   (in-memory, lost on restart — stub only)
_STORE: dict[str, list[dict]] = {}

_VALID_STATUSES = {"pending", "blocked", "active", "done", "cancelled"}


def _tasks_for(session_context: dict) -> list[dict]:
    user_id = session_context.get("user_id", "local-user")
    return _STORE.setdefault(user_id, [])


def _find_task(tasks: list[dict], needle: str) -> dict | None:
    needle = (needle or "").strip().lower()
    if not needle:
        return None
    # exact id match first, then fuzzy title contains
    for t in tasks:
        if t["id"] == needle:
            return t
    for t in tasks:
        if needle in t["title"].lower():
            return t
    return None


async def create_task(args: dict, session_context: dict) -> dict:
    tasks = _tasks_for(session_context)

    parent = _find_task(tasks, args.get("parent_task", ""))
    task = {
        "id": uuid.uuid4().hex[:8],
        "title": args.get("title", "Untitled task"),
        "description": args.get("description", ""),
        "status": "pending",
        "due_at": args.get("due_at"),
        "parent_id": parent["id"] if parent else None,
        "needs_research": bool(args.get("needs_research", False)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    tasks.append(task)
    logger.info("STUB create_task → %s (%s)", task["title"], task["id"])

    nested = f" (nested under '{parent['title']}')" if parent else ""
    return {
        "ok": True,
        "task": task,
        "summary": f"Created task '{task['title']}'{nested}.",
    }


async def query_tasks(args: dict, session_context: dict) -> dict:
    tasks = _tasks_for(session_context)
    scope = args.get("scope", "all_active")

    if scope == "by_status":
        wanted = args.get("status_filter", "pending")
        matches = [t for t in tasks if t["status"] == wanted]
    elif scope == "specific_task":
        found = _find_task(tasks, args.get("search_text", ""))
        matches = [found] if found else []
    elif scope == "all_active":
        matches = [t for t in tasks if t["status"] not in ("done", "cancelled")]
    else:
        # today / this_week / this_month — stub returns all active (no real date filtering yet)
        matches = [t for t in tasks if t["status"] not in ("done", "cancelled")]

    brief = [
        {"id": t["id"], "title": t["title"], "status": t["status"], "due_at": t["due_at"]}
        for t in matches
    ]
    if brief:
        titles = ", ".join(t["title"] for t in brief)
        summary = f"Found {len(brief)} task(s): {titles}."
    else:
        summary = "No matching tasks found."
    logger.info("STUB query_tasks scope=%s → %d task(s)", scope, len(brief))
    return {"ok": True, "tasks": brief, "summary": summary}


async def update_task_status(args: dict, session_context: dict) -> dict:
    tasks = _tasks_for(session_context)
    task = _find_task(tasks, args.get("task", ""))
    if task is None:
        return {"ok": False, "error": "task_not_found", "summary": "I couldn't find that task."}

    new_status = args.get("new_status", "")
    if new_status not in _VALID_STATUSES:
        return {"ok": False, "error": "invalid_status", "summary": f"'{new_status}' is not a valid status."}

    task["status"] = new_status
    if args.get("note"):
        task["note"] = args["note"]
    logger.info("STUB update_task_status → %s = %s", task["title"], new_status)

    # Dependency unblocking (M2 will do this against the DB; harmless stub version here)
    unblocked = []
    if new_status == "done":
        for t in tasks:
            if t.get("depends_on_id") == task["id"] and t["status"] == "blocked":
                t["status"] = "pending"
                unblocked.append(t["title"])

    extra = f" Unblocked: {', '.join(unblocked)}." if unblocked else ""
    return {
        "ok": True,
        "task": {"id": task["id"], "title": task["title"], "status": task["status"]},
        "summary": f"Marked '{task['title']}' as {new_status}.{extra}",
    }
