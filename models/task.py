"""
Task model — a single self-referential table supporting arbitrary-depth task
trees plus a separate sequencing dependency.

Two distinct relationships (deliberately separate):
  • parent_id      — hierarchy/grouping (a sub-step belongs to a goal)
  • depends_on_id  — sequencing (this task is blocked until another is done)

A JLPT-style goal happens to use both; a general task may use neither, either,
or both. Enum-like fields are plain strings validated at the service layer
(simpler migrations, portable for testing).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True, nullable=False)

    parent_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    depends_on_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tasks.id"), nullable=True
    )

    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # single_step | milestone
    task_type: Mapped[str] = mapped_column(String, default="single_step")
    # pending | blocked | active | done | cancelled
    status: Mapped[str] = mapped_column(String, default="pending", index=True)

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set once a due reminder for this task has been spoken to the user, so a
    # proactive sweep never re-announces the same reminder (Milestone 4).
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # manual_confirm | auto_detect  (only manual is implemented today)
    completion_condition: Mapped[str] = mapped_column(String, default="manual_confirm")
    requires_research: Mapped[bool] = mapped_column(Boolean, default=False)

    # Freeform structured data (research output, portal links, etc.).
    # JSONB on Postgres, JSON elsewhere (for portability/testing).
    context: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_brief(self) -> dict:
        """Compact dict for tool results / API responses."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "task_type": self.task_type,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "parent_id": self.parent_id,
        }
