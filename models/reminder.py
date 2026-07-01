"""
Reminder ledger — one row per scheduled notification "fire" for a task.

This is the delivery backbone for proactive push notifications. It is a true
work-queue / ledger, deliberately separate from the task's own `due_at`:

  • A task carries the canonical event time (`tasks.due_at`).
  • Each Reminder row is ONE notification to send at `fire_at` (= due_at minus an
    offset). A task can therefore have several reminders — e.g. one 10 minutes
    before the meeting and one at the meeting time (offsets 10 and 0).

The status field drives a small state machine that gives the two guarantees the
delivery sweep relies on:

    pending ──claim──▶ claimed ──FCM ok──▶ sent ──mobile ack──▶ (delivered_at set)
       ▲                  │
       └── retry / reclaim ┘ (transient failure, or a crashed claim timing out)
                          │
                          └── attempts ≥ max ──▶ failed

  • Zero-DUP: a row only leaves `pending` via an atomic claim
    (SELECT … FOR UPDATE SKIP LOCKED), so no two sweep runs/workers ever send the
    same reminder.
  • Zero-LOSS: a row is marked `sent` ONLY after FCM accepts it; transient
    failures return it to `pending`, and a claim that never completes (crash) is
    reclaimed once `claimed_at` ages past the claim timeout. The only terminal
    non-delivery is `failed` (attempts exhausted), which is logged — never a
    silent drop.

`UNIQUE(task_id, offset_minutes)` means re-syncing a task (e.g. its due date
changed) updates the existing row in place rather than ever duplicating it.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    String,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Status values (plain strings, validated at the service layer — same convention
# the Task model uses, for simpler migrations and portable testing).
PENDING = "pending"
CLAIMED = "claimed"
SENT = "sent"
FAILED = "failed"
CANCELLED = "cancelled"


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        # One reminder per (task, offset) — re-sync updates in place, never dupes.
        UniqueConstraint("task_id", "offset_minutes", name="uq_reminder_task_offset"),
        # The claim query filters on (status, fire_at); index it for the sweep.
        Index("ix_reminders_status_fire_at", "status", "fire_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)

    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Denormalized so the delivery sweep can fetch the user's device tokens
    # without a join back through tasks.
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True, nullable=False
    )

    # The UTC instant this reminder should fire (= task.due_at - offset_minutes).
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # Minutes BEFORE due_at. 0 = at the event time; 10 = ten minutes before, etc.
    offset_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Optional human copy hint (e.g. "Starting in 10 minutes").
    label: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(String, default=PENDING, index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by the mobile client when the notification actually surfaced — true
    # end-to-end delivery proof, beyond "FCM accepted it".
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
