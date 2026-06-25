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
from datetime import datetime, timezone, timedelta

from openai import AsyncOpenAI
from sqlalchemy import select

from core.config import settings
from db.session import async_session
from models.task import Task
from services.research.research_service import ResearchService

logger = logging.getLogger(__name__)

_research = ResearchService()
_llm = AsyncOpenAI(
    api_key=settings.OPENROUTER_API_KEY,
    base_url=settings.OPENROUTER_BASE_URL,
    timeout=30.0,
    max_retries=2,
)

_OPEN_STATES = ("pending", "blocked", "active")

_DECIDE_PROMPT = (
    "You compare a tracked task against fresh research findings and decide whether "
    "anything actionable changed since it was last looked at (e.g. a registration "
    "window opened, a deadline or exam date was set or moved). Today's date will "
    "be given. A SUCCESS CONDITION will also be given — check whether the research "
    "findings satisfy it. Reply with ONLY a JSON object: {\"changed\": bool, "
    "\"success\": bool, \"new_due_at\": ISO-8601 datetime or null, \"note\": short "
    "human-readable summary of what changed (empty string if nothing)}. Set "
    "changed=true and success=true only for concrete, useful updates that match "
    "the success condition — not vague or unchanged information."
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
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _due_for_attempt(task: Task, now: datetime) -> bool:
    """Structured tasks only re-poll once their scheduled next_attempt_at passes
    — so a 7-day retry interval actually skips the web-search call for 7 days
    instead of burning one every daily sweep."""
    intent = (task.context or {}).get("research_intent") or {}
    next_at = _parse_dt(intent.get("next_attempt_at"))
    return next_at is None or next_at <= now


def _store_findings(row: Task, summary: str, links: list, note: str) -> None:
    """Write fresh research into the CANONICAL context.research slot — the same
    place create_task uses and that the pipeline's task-injection + to_brief
    surface — so a link found by the daily poll is retrievable next time the
    user asks. Plus an audit stamp under research_refresh."""
    ctx = dict(row.context or {})
    ctx["research"] = {"summary": summary, "links": links or []}
    ctx["research_refresh"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    }
    row.context = ctx


async def _decide(task: Task, findings: str, success_condition: str = "") -> dict:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    prompt = (
        f"Today's date is {today}.\n\n"
        f"Task: {task.title}\n"
        f"Description: {task.description or '(none)'}\n"
        f"Current due date: {task.due_at.isoformat() if task.due_at else '(none)'}\n"
        f"Success condition: {success_condition or '(any concrete update)'}\n\n"
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

    now = datetime.now(timezone.utc)
    logger.info("Daily refresh: %d watched task(s) for %s", len(tasks), user_id)
    updated = 0

    for task in tasks:
        ctx = task.context or {}
        intent = ctx.get("research_intent") or {}
        structured = bool(intent.get("query"))

        # Structured tasks honour their own retry cadence — skip (no API call)
        # until next_attempt_at passes. Legacy tasks (no intent) poll each sweep.
        if structured and not _due_for_attempt(task, now):
            continue

        query = intent.get("query") or (
            f"Latest status update for: {task.title}. "
            f"{task.description or ''} "
            "Have registrations/applications opened, and what are the current "
            "official dates and deadlines?"
        ).strip()
        success_condition = intent.get("success_condition", "")
        retry_days = int(intent.get("retry_interval_days") or 7)

        try:
            result = await _research.research(query)
            decision = await _decide(task, result["summary"], success_condition)
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh failed for task %s: %s", task.id, exc)
            continue

        # "Resolved" = success condition met (structured) or something concrete
        # changed (legacy, where there's no explicit condition).
        resolved = decision.get("success") if structured else decision.get("changed")

        if not resolved:
            # Not yet. Schedule the next attempt deterministically via
            # next_attempt_at — NOT by mutating the user-facing due_at.
            if structured:
                async with async_session() as session:
                    row = await session.get(Task, task.id)
                    if row is not None:
                        rctx = dict(row.context or {})
                        rint = dict(rctx.get("research_intent") or {})
                        rint["next_attempt_at"] = (now + timedelta(days=retry_days)).isoformat()
                        rctx["research_intent"] = rint
                        row.context = rctx
                        await session.commit()
                logger.info(
                    "Research retry: '%s' condition not met — next attempt in %dd",
                    task.title, retry_days,
                )
            continue

        # Resolved — store findings canonically, apply any new date, STOP polling.
        async with async_session() as session:
            row = await session.get(Task, task.id)
            if row is None:
                continue
            new_due = _parse_dt(decision.get("new_due_at"))
            if new_due is not None:
                row.due_at = new_due
                row.last_reminded_at = None  # fresh date → re-announceable
            _store_findings(row, result["summary"], result["links"], decision.get("note", ""))
            if structured:
                row.requires_research = False  # got what we waited for; stop polling
            await session.commit()
        updated += 1
        logger.info("Daily refresh resolved '%s': %s", task.title, decision.get("note", ""))

    return updated
