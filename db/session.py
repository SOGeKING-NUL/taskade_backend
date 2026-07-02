"""
Async database engine + session factory.

For now `init_db()` creates tables directly (create_all) so the app runs with no
migration step. Alembic will be introduced in Milestone 5, when the schema first
needs to evolve (profiles + pgvector), with a baseline migration.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings
from models import Base  # noqa: F401  (registers tables on Base.metadata)

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,      # drop dead connections (e.g. pooler recycled them)
    pool_size=10,
    max_overflow=10,         # up to 20 concurrent; mind Supabase pooler's own ceiling
    pool_timeout=10,         # fail loudly after 10s instead of hanging forever
    pool_recycle=1800,       # recycle connections every 30 min
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Ensure all tables exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all() never alters existing tables — patch in columns added
        # after the table first existed. Alembic arrives properly in M5.
        await conn.execute(
            text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_reminded_at TIMESTAMPTZ")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR")
        )
        await conn.execute(
            text("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS location VARCHAR")
        )
        # First-run onboarding gate — existing rows backfill to FALSE so they get
        # onboarded on next app open.
        await conn.execute(
            text(
                "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS "
                "onboarding_complete BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        # Once-per-local-day guard for the daily check-in push.
        await conn.execute(
            text("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_checkin_on DATE")
        )
        # ── Temporal Knowledge Graph patches (Milestone 6) ───────────
        await conn.execute(
            text("ALTER TABLE entity_edges ADD COLUMN IF NOT EXISTS target_date TIMESTAMPTZ")
        )
        await conn.execute(
            text("ALTER TABLE entity_edges ADD COLUMN IF NOT EXISTS horizon_scale VARCHAR DEFAULT 'medium'")
        )
    logger.info("Database ready — tables ensured")
