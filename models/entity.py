"""
Entity & EntityEdge — the temporal knowledge graph (Milestone 6).

Replaces flat ``user_memories`` with a structured, time-aware graph:

    Entity   — a semantic node: a person, place, goal, habit, topic, or event
               the user has mentioned across conversations.
    EntityEdge — a typed, timestamped relationship between two entities
               (subject → predicate → object).  Supports:
                 • Temporal invalidation: when a fact changes, old edges get a
                   ``valid_until`` stamp instead of being deleted — the system can
                   reason about *when* something stopped being true.
                 • Differential decay: ``horizon_scale`` encodes how long silence
                   is "normal" for this kind of relationship (daily vs. monthly),
                   so the reflection job doesn't flag a long-term goal as
                   abandoned just because the user didn't mention it today.

Together these two tables let Postgres act as a lightweight temporal knowledge
graph — no Neo4j or external graph DB required.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Text, DateTime, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Entity(Base):
    """A semantic node in the user's knowledge graph."""
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True, nullable=False
    )

    # person | place | goal | habit | topic | event
    type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Short LLM-generated description, updated as the system learns more.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class EntityEdge(Base):
    """A timestamped, directed relationship between two entities."""
    __tablename__ = "entity_edges"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True, nullable=False
    )

    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[str] = mapped_column(
        String, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The predicate: "is_studying", "plans_to_attend", "lives_in", etc.
    relation: Mapped[str] = mapped_column(String, nullable=False)
    # Optional human-readable version of the full triple.
    fact_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Temporal validity ────────────────────────────────────────────────
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # NULL = still valid ("open-ended truth").
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # If this edge was invalidated by a newer edge, point to it.
    invalidated_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("entity_edges.id"), nullable=True
    )

    # ── Differential decay ───────────────────────────────────────────────
    # How long this relationship is expected to stay relevant without
    # re-mention.  Drives the reflection job's multi-scale sweep thresholds.
    # micro (days) | short (weeks) | medium (months) | long (years)
    horizon_scale: Mapped[str] = mapped_column(String, default="medium")

    # Optional ISO target date for goal-type edges (e.g. "JLPT in December").
    target_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # LLM confidence in the extraction (0.0 – 1.0).
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
