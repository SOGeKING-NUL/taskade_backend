"""
Graph service — entity resolution, temporal invalidation, and graph extraction.

This is the write-path for the Temporal Knowledge Graph.  Called from the
existing ``memory_service.remember()`` flow (fire-and-forget, async), it:

1. Prompts an LLM to extract subject–predicate–object triples + mood signals
   from the latest batch of user↔assistant exchanges.
2. Resolves each extracted entity against the user's existing graph nodes
   (trigram/case-insensitive match + LLM disambiguation).
3. Inserts new entities and edges; when a new edge *contradicts* an existing
   one (same source + relation but different target/fact), the old edge is
   temporally invalidated (``valid_until = now``) rather than deleted.
4. Stores any extracted mood signals.

All DB work happens in its own async session — never on the hot voice path.
"""

import json
import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select, func

from core.config import settings
from db.session import async_session
from models.entity import Entity, EntityEdge
from models.reflection import MoodSignal

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=settings.OPENROUTER_API_KEY,
    base_url=settings.OPENROUTER_BASE_URL,
    timeout=30.0,
    max_retries=2,
)

# ── Extraction prompt ────────────────────────────────────────────────────
_EXTRACT_GRAPH_PROMPT = (
    "You extract structured knowledge from a voice-assistant conversation. "
    "From the exchange below, produce a JSON object with two keys:\n\n"
    "1. `triples`: an array of objects, each with:\n"
    "   - `subject`: {`name`: str, `type`: person|place|goal|habit|topic|event}\n"
    "   - `predicate`: a short verb phrase (e.g. 'is_studying', 'plans_to_attend', 'lives_in')\n"
    "   - `object`: {`name`: str, `type`: person|place|goal|habit|topic|event}\n"
    "   - `fact_text`: a short third-person sentence summarizing the triple\n"
    "   - `horizon_scale`: micro|short|medium|long — how long this fact is expected "
    "to stay relevant without re-mention (micro=days, short=weeks, medium=months, long=years)\n"
    "   - `target_date`: ISO 8601 date if a concrete deadline/event date is mentioned, else null\n"
    "   - `confidence`: float 0.0–1.0\n\n"
    "2. `mood_signals`: an array of objects, each with:\n"
    "   - `entity_name`: the entity this mood is about (must match a subject or object above)\n"
    "   - `valence`: float -1.0 to 1.0\n"
    "   - `label`: a word like 'excited', 'frustrated', 'anxious', 'confident', 'neutral'\n\n"
    "Rules:\n"
    "- The 'user' themselves should always be represented as an entity with "
    "name='user' and type='person'.\n"
    "- Only extract DURABLE facts worth remembering across sessions. Skip "
    "pleasantries, one-off requests, and transient context.\n"
    "- Keep entity names consistent and lowercase (e.g. 'jlpt n5', not 'JLPT N5 Exam').\n"
    "- If nothing worth remembering, return empty arrays.\n"
    "- Return ONLY the JSON object, no other text."
)


# ── Entity resolution ────────────────────────────────────────────────────
async def _resolve_entity(
    session, user_id: str, name: str, entity_type: str
) -> Entity:
    """Find or create an entity, matching by case-insensitive name."""
    name_lower = name.strip().lower()

    existing = (
        await session.execute(
            select(Entity).where(
                Entity.user_id == user_id,
                func.lower(Entity.name) == name_lower,
            )
        )
    ).scalars().first()

    if existing is not None:
        return existing

    entity = Entity(user_id=user_id, type=entity_type, name=name_lower)
    session.add(entity)
    await session.flush()
    logger.info("New entity: '%s' (%s) for %s", name_lower, entity_type, user_id)
    return entity


# ── Temporal invalidation ────────────────────────────────────────────────
async def _invalidate_contradictions(
    session, user_id: str, source_id: str, relation: str, new_edge_id: str
) -> int:
    """Mark older edges with the same source+relation as invalid."""
    now = datetime.now(timezone.utc)
    old_edges = (
        await session.execute(
            select(EntityEdge).where(
                EntityEdge.user_id == user_id,
                EntityEdge.source_id == source_id,
                EntityEdge.relation == relation,
                EntityEdge.valid_until.is_(None),  # still valid
                EntityEdge.id != new_edge_id,       # not the one we just created
            )
        )
    ).scalars().all()

    for edge in old_edges:
        edge.valid_until = now
        edge.invalidated_by = new_edge_id

    if old_edges:
        await session.flush()
    return len(old_edges)


# ── Date parsing helper ──────────────────────────────────────────────────
def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ── Main extraction + ingestion ──────────────────────────────────────────
async def extract_and_store(user_id: str, user_text: str, assistant_text: str) -> int:
    """
    Extract entities/edges/mood from an exchange and store them in the graph.

    Returns the number of new edges created.  Fire-and-forget — exceptions are
    caught and logged, never propagated to the caller.
    """
    exchange = f"User: {user_text}\nAssistant: {assistant_text}"

    # ── Step 1: LLM extraction ───────────────────────────────────────────
    try:
        resp = await _client.chat.completions.create(
            model=settings.OPENROUTER_LLM_MODEL,
            messages=[
                {"role": "system", "content": _EXTRACT_GRAPH_PROMPT},
                {"role": "user", "content": exchange},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        payload = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph extraction LLM call failed: %s", exc)
        return 0

    triples = payload.get("triples") or []
    mood_signals = payload.get("mood_signals") or []

    if not triples and not mood_signals:
        return 0

    # ── Step 2: Resolve entities + create edges ──────────────────────────
    edges_created = 0

    async with async_session() as session:
        # Build a lookup of resolved entities for mood signal matching.
        resolved: dict[str, Entity] = {}

        for triple in triples:
            try:
                subj_info = triple.get("subject") or {}
                obj_info = triple.get("object") or {}
                subj_name = (subj_info.get("name") or "").strip()
                obj_name = (obj_info.get("name") or "").strip()
                predicate = (triple.get("predicate") or "").strip()

                if not subj_name or not obj_name or not predicate:
                    continue

                # Resolve subject
                if subj_name not in resolved:
                    resolved[subj_name] = await _resolve_entity(
                        session, user_id, subj_name, subj_info.get("type", "topic")
                    )
                source = resolved[subj_name]

                # Resolve object
                if obj_name not in resolved:
                    resolved[obj_name] = await _resolve_entity(
                        session, user_id, obj_name, obj_info.get("type", "topic")
                    )
                target = resolved[obj_name]

                # Create edge
                edge = EntityEdge(
                    user_id=user_id,
                    source_id=source.id,
                    target_id=target.id,
                    relation=predicate,
                    fact_text=triple.get("fact_text"),
                    horizon_scale=triple.get("horizon_scale", "medium"),
                    target_date=_parse_dt(triple.get("target_date")),
                    confidence=triple.get("confidence"),
                )
                session.add(edge)
                await session.flush()

                # Invalidate contradictions (same source + relation but older)
                invalidated = await _invalidate_contradictions(
                    session, user_id, source.id, predicate, edge.id
                )
                if invalidated:
                    logger.info(
                        "Invalidated %d old edge(s) for %s -[%s]->",
                        invalidated, subj_name, predicate,
                    )
                edges_created += 1

            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to process triple: %s — %s", triple, exc)
                continue

        # ── Step 3: Store mood signals ───────────────────────────────────
        for ms in mood_signals:
            try:
                entity_name = (ms.get("entity_name") or "").strip().lower()
                entity = resolved.get(entity_name)
                signal = MoodSignal(
                    user_id=user_id,
                    entity_id=entity.id if entity else None,
                    valence=float(ms.get("valence", 0.0)),
                    label=ms.get("label"),
                )
                session.add(signal)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to store mood signal: %s — %s", ms, exc)

        await session.commit()

    if edges_created:
        logger.info(
            "Graph extraction: %d edge(s), %d mood signal(s) for %s",
            edges_created, len(mood_signals), user_id,
        )
    return edges_created


# ── Read helpers (used by _memory_context and reflection jobs) ───────────
async def get_entities(session, user_id: str, limit: int = 50) -> list[Entity]:
    """Get the user's most recently updated entities."""
    rows = (
        await session.execute(
            select(Entity)
            .where(Entity.user_id == user_id)
            .order_by(Entity.updated_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def get_active_edges(
    session, user_id: str, limit: int = 50
) -> list[EntityEdge]:
    """Get currently valid (non-invalidated) edges for the user."""
    rows = (
        await session.execute(
            select(EntityEdge)
            .where(
                EntityEdge.user_id == user_id,
                EntityEdge.valid_until.is_(None),
            )
            .order_by(EntityEdge.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def get_entity_name_map(session, user_id: str) -> dict[str, str]:
    """Return {entity_id: entity_name} for all of a user's entities."""
    rows = (
        await session.execute(
            select(Entity.id, Entity.name).where(Entity.user_id == user_id)
        )
    ).all()
    return {r[0]: r[1] for r in rows}
