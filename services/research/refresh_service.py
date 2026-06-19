"""
Daily research refresh (Milestone 5 — proactive intelligence).

The "6am check" the user asked for: once per day, at the user's configured local
hour, re-research the tasks that are waiting on a real-world external event
(`requires_research=True`, still open) — e.g. "have JLPT registrations opened on
the portal yet?", "did the deadline move?". If research surfaces a concrete
change, update the task: adjust `due_at`, stamp the fresh findings into its
`context`, and add a note. The existing reminder system then naturally surfaces
anything that just became due.

This is LLM-heavy (a web-search call per watched task), which is exactly why it
runs ~once a day on a CronTrigger — never on the hot conversational path.
"""

import json
import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select

from core.config import settings
from db.session import async_session
from models.task import Task
from services.research.research_service import ResearchService

logger = logging.getLogger(__name__)

_research = ResearchService()
_llm = AsyncOpenAI(api_key=settings.OPENROUTER_API_KEY, base_url=settings.OPENROUTER_BASE_URL)

_OPEN_STATES = ("pending", "blocked", "active")

_DECIDE_PROMPT = (
    "You compare a tracked task against fresh research findings and decide whether "
    "anything actionable changed since it was last looked at (e.g. a registration "
    "window opened, a deadline or exam date was set or moved). Today's date will "
    "be given. Reply with ONLY a JSON object: {\"changed\": bool, \"new_due_at\": "
    "ISO-8601 datetime or null, \"note\": short human-readable summary of what "
    "changed (empty string if nothing)}. Only set changed=true for concrete, "
    "useful updates — not vague or unchanged information."
)


async def _watched_tasks(user_id: str) -> list[Task]:
    async with async_session() as session:
        rows = (
            await session.execute(
                select(Task).where(
                    Task.user_id == user_id,
                    Task.requires_research.is_(True),
                    Task.status.in_(_OPEN_STATES),
                )
            )
        ).scalars().all()
        return list(rows)


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def _decide(task: Task, findings: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    prompt = (
        f"Today's date is {today}.\n\n"
        f"Task: {task.title}\n"
        f"Description: {task.description or '(none)'}\n"
        f"Current due date: {task.due_at.isoformat() if task.due_at else '(none)'}\n\n"
        f"Fresh research findings:\n{findings}"
    )
    resp = await _llm.chat.completions.create(
        model=settings.OPENROUTER_LLM_MODEL,
        messages=[
            {"role": "system", "content": _DECIDE_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return json.loads(resp.choices[0].message.content or "{}")


async def refresh_watched_tasks(user_id: str) -> int:
    """Re-research every watched task; apply concrete updates. Returns #updated."""
    tasks = await _watched_tasks(user_id)
    if not tasks:
        return 0

    logger.info("Daily refresh: %d watched task(s) for %s", len(tasks), user_id)
    updated = 0

    for task in tasks:
        query = (
            f"Latest status update for: {task.title}. "
            f"{task.description or ''} "
            "Have registrations/applications opened, and what are the current "
            "official dates and deadlines?"
        ).strip()
        try:
            result = await _research.research(query)
            decision = await _decide(task, result["summary"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh failed for task %s: %s", task.id, exc)
            continue

        if not decision.get("changed"):
            continue

        # Persist the update on a fresh session/row to avoid stale state.
        async with async_session() as session:
            row = await session.get(Task, task.id)
            if row is None:
                continue
            new_due = _parse_dt(decision.get("new_due_at"))
            if new_due is not None:
                row.due_at = new_due
                # A fresh due date means it may need re-announcing.
                row.last_reminded_at = None
            ctx = dict(row.context or {})
            ctx["research_refresh"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "note": decision.get("note", ""),
                "summary": result["summary"],
                "links": result["links"],
            }
            row.context = ctx
            await session.commit()
        updated += 1
        logger.info("Daily refresh updated '%s': %s", task.title, decision.get("note", ""))

    return updated
