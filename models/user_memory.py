"""
UserMemory — free-form durable facts/preferences extracted from conversation.

The unstructured half of Milestone 5 memory: one row per remembered fact
("prefers morning study sessions", "is preparing for JLPT N5", "dislikes long
answers"). Written fire-and-forget after a turn by an LLM extraction pass;
recalled via a fast DB read and injected into the tool-calling LLM's context.

Kept intentionally backend-agnostic — a row of text + a kind tag. A future
upgrade to vector similarity (pgvector/Mem0) can add an embedding column without
changing the `remember()`/`recall()` contract callers depend on.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserMemory(Base):
    __tablename__ = "user_memories"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True, nullable=False
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    # preference | fact | goal — a coarse tag, not enforced at the DB layer.
    kind: Mapped[str] = mapped_column(String, default="fact")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
