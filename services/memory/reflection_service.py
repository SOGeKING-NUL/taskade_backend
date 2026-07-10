"""
Reflection service — state-delta detection across the temporal knowledge graph.

This is NOT psychological trait inference.  It is a mechanical diff of the
entity graph that produces factual observations about what changed:

    "JLPT mentions stopped 10 days ago; exam is in 3 weeks."
    "Marathon training edge is still active; last mentioned 2 days ago."
    "Driver's test was completed yesterday."

Three sweep scales run at different cadences:

    daily   — checks micro/short-horizon edges (things that should be mentioned
              every few days if they're still active).
    weekly  — checks medium-horizon edges (goals expected to span weeks/months).
    monthly — checks long-horizon edges (multi-month or yearly goals).

Each sweep produces 0-N ``Reflection`` rows consumed by the greeting API and
injected into the session context at connect time.
"""

import logging
from datetime import datetime, timezone, timedelta

from openai import AsyncOpenAI
from sqlalchemy import select, func

from core.config import settings
from db.session import async_session
from models.entity import Entity, EntityEdge
from models.reflection import Reflection, MoodSignal

logger = logging.getLogger(__name__)

# OpenRouter (not the hot-path Gemini brain) handles all background LLM work.
_client = AsyncOpenAI(
    api_key=settings.OPENROUTER_API_KEY,
    base_url=settings.OPENROUTER_BASE_URL,
    timeout=30.0,
    max_retries=2,
)

# How long silence is "suspicious" for each horizon scale.
_SILENCE_THRESHOLDS = {
    "micro": timedelta(days=2),
    "short": timedelta(days=7),
    "medium": timedelta(weeks=3),
    "long": timedelta(days=60),
}

# Map sweep types to which horizon scales they check.
_SWEEP_HORIZONS = {
    "daily": ["micro", "short"],
    "weekly": ["medium"],
    "monthly": ["long"],
}

_REFLECTION_PROMPT = (
    "You are analyzing changes in a user's knowledge graph for a voice assistant. "
    "Given the following active relationships (edges) and their last-activity timestamps, "
    "generate 1-3 SHORT, FACTUAL observations about what has changed or gone quiet. "
    "These are NOT psychological insights — they are mechanical state-deltas:\n\n"
    "GOOD examples:\n"
    "- 'JLPT prep hasn't been mentioned in 12 days; exam is Dec 1.'\n"
    "- 'Marathon training is still active; last mentioned 2 days ago.'\n"
    "- 'Driver test goal was completed.'\n\n"
    "BAD examples (never do this):\n"
    "- 'User seems anxious about the exam.'\n"
    "- 'User tends to procrastinate on long-term goals.'\n\n"
    "Reply with a JSON object: {\"reflections\": [str, ...]}. "
    "If nothing notable, return an empty array."
)


async def _get_edges_for_sweep(
    session, user_id: str, horizon_scales: list[str]
) -> list[EntityEdge]:
    """Get active edges matching the given horizon scales."""
    rows = (
        await session.execute(
            select(EntityEdge).where(
                EntityEdge.user_id == user_id,
                EntityEdge.valid_until.is_(None),
                EntityEdge.horizon_scale.in_(horizon_scales),
            )
        )
    ).scalars().all()
    return list(rows)


async def _get_recent_mood(session, user_id: str, limit: int = 10) -> list[MoodSignal]:
    """Get most recent mood signals for context."""
    rows = (
        await session.execute(
            select(MoodSignal)
            .where(MoodSignal.user_id == user_id)
            .order_by(MoodSignal.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


def _edge_summary(edge: EntityEdge, names: dict[str, str], now: datetime) -> str:
    """Build a human-readable summary of an edge for the LLM prompt."""
    source = names.get(edge.source_id, "?")
    target = names.get(edge.target_id, "?")
    age = now - edge.created_at
    age_str = f"{age.days}d ago" if age.days > 0 else "today"

    target_info = ""
    if edge.target_date:
        days_until = (edge.target_date - now).days
        target_info = f" (target date: {edge.target_date.strftime('%b %d')}, {days_until}d away)"

    return (
        f"{source} -[{edge.relation}]-> {target} | "
        f"created {age_str}{target_info} | "
        f"horizon: {edge.horizon_scale}"
    )


async def run_sweep(user_id: str, sweep_type: str = "daily") -> int:
    """
    Run a single reflection sweep for a user.

    Checks edges whose horizon_scale matches this sweep type, identifies
    suspiciously quiet ones, and generates reflection insights.

    Returns the number of reflections created.
    """
    horizons = _SWEEP_HORIZONS.get(sweep_type, ["micro", "short"])
    now = datetime.now(timezone.utc)

    async with async_session() as session:
        edges = await _get_edges_for_sweep(session, user_id, horizons)
        if not edges:
            return 0

        # Build entity name map for readable summaries.
        entity_ids = set()
        for e in edges:
            entity_ids.add(e.source_id)
            entity_ids.add(e.target_id)

        name_rows = (
            await session.execute(
                select(Entity.id, Entity.name).where(Entity.id.in_(entity_ids))
            )
        ).all()
        names = {r[0]: r[1] for r in name_rows}

        # Build edge summaries for the LLM.
        edge_lines = []
        notable_entity_ids = set()
        for edge in edges:
            threshold = _SILENCE_THRESHOLDS.get(edge.horizon_scale, timedelta(days=7))
            age = now - edge.created_at
            # Flag edges that have been silent longer than expected.
            is_quiet = age > threshold
            line = _edge_summary(edge, names, now)
            if is_quiet:
                line += " ⚠️ SILENT beyond threshold"
            edge_lines.append(line)
            notable_entity_ids.add(edge.source_id)
            notable_entity_ids.add(edge.target_id)

    if not edge_lines:
        return 0

    # Ask the LLM to generate reflections.
    prompt = (
        f"Sweep type: {sweep_type}\n"
        f"Current date: {now.strftime('%A, %d %B %Y')}\n\n"
        f"Active edges:\n" + "\n".join(f"- {line}" for line in edge_lines)
    )

    try:
        resp = await _client.chat.completions.create(
            model=settings.OPENROUTER_LLM_MODEL,
            messages=[
                {"role": "system", "content": _REFLECTION_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        import json
        payload = json.loads(resp.choices[0].message.content or "{}")
        reflections = payload.get("reflections") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("reflection sweep failed for %s: %s", user_id, exc)
        return 0

    if not reflections:
        return 0

    # Store reflections.
    async with async_session() as session:
        for content in reflections:
            content = (content if isinstance(content, str) else str(content)).strip()
            if not content:
                continue
            session.add(Reflection(
                user_id=user_id,
                content=content,
                sweep_type=sweep_type,
                supporting_entity_ids=list(notable_entity_ids)[:10],
            ))
        await session.commit()

    logger.info(
        "Reflection sweep (%s) for %s: %d insight(s)",
        sweep_type, user_id, len(reflections),
    )
    return len(reflections)


# ── Read helpers ─────────────────────────────────────────────────────────
async def get_recent_reflections(
    session, user_id: str, limit: int = 5
) -> list[Reflection]:
    """Get the most recent reflections for session context / greeting."""
    rows = (
        await session.execute(
            select(Reflection)
            .where(Reflection.user_id == user_id)
            .order_by(Reflection.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def get_recent_mood_summary(
    session, user_id: str, limit: int = 5
) -> list[dict]:
    """Get recent mood signals with entity names for context injection."""
    signals = await _get_recent_mood(session, user_id, limit)
    if not signals:
        return []

    entity_ids = [s.entity_id for s in signals if s.entity_id]
    names = {}
    if entity_ids:
        name_rows = (
            await session.execute(
                select(Entity.id, Entity.name).where(Entity.id.in_(entity_ids))
            )
        ).all()
        names = {r[0]: r[1] for r in name_rows}

    return [
        {
            "entity": names.get(s.entity_id, "general"),
            "valence": s.valence,
            "label": s.label,
        }
        for s in signals
    ]
