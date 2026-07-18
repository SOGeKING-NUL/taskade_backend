"""
Memory service — the single long-term store (`remember` / `recall`).

`user_memories` is the one durable "what we know about the user" store. The
former parallel knowledge-graph + reflection layers were removed in the memory
revamp (see docs/db_revamp.md) — one store, single-pass extraction.

Interface contract (kept backend-agnostic so a future swap to mem0/pgvector
vector search touches only this file):

    recall(session, user_id, limit)  -> list[str]   # fast DB read, hot path
    remember(user_id, user_text, assistant_text)    # fire-and-forget, own session
"""

import json
import logging

from openai import AsyncOpenAI

from core.config import settings
from db.session import async_session
from models.user_memory import UserMemory
from sqlalchemy import select

logger = logging.getLogger(__name__)

# Shared client (OpenRouter is OpenAI-compatible). Reused across calls.
_client = AsyncOpenAI(
    api_key=settings.OPENROUTER_API_KEY,
    base_url=settings.OPENROUTER_BASE_URL,
    timeout=30.0,
    max_retries=2,
)

_EXTRACT_SYSTEM_PROMPT = (
    "You extract durable personal facts about the user from a single exchange "
    "in a voice assistant. Return ONLY long-lived facts, stable preferences, or "
    "goals worth remembering across future conversations — NOT one-off requests, "
    "task details (those are stored separately), pleasantries, or transient "
    "context. Examples worth keeping: 'is preparing for the JLPT N5 exam', "
    "'prefers concise answers', 'studies best in the early morning', 'is based "
    "in Pune'. \n\n"
    "Reply with a JSON object: {\"memories\": [{\"content\": str, \"kind\": "
    "\"preference\"|\"fact\"|\"goal\"}]}. Use an empty list if nothing is worth "
    "remembering. Keep each `content` a short third-person statement."
)


async def recall(session, user_id: str, limit: int | None = None) -> list[str]:
    """Return recent remembered facts for the user (fast, no LLM)."""
    limit = limit or settings.MEMORY_RECALL_LIMIT
    rows = (
        await session.execute(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [r.content for r in rows]


async def _flat_remember(user_id: str, user_text: str, assistant_text: str) -> None:
    """Original flat-fact extraction (backward compat, will be retired)."""
    exchange = f"User: {user_text}\nAssistant: {assistant_text}"
    try:
        resp = await _client.chat.completions.create(
            model=settings.OPENROUTER_LLM_MODEL,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": exchange},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        payload = json.loads(resp.choices[0].message.content or "{}")
        candidates = payload.get("memories", []) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("flat memory extraction failed: %s", exc)
        return

    if not candidates:
        return

    async with async_session() as session:
        existing = {c.lower() for c in await recall(session, user_id, limit=200)}
        added = 0
        for m in candidates:
            content = (m.get("content") or "").strip()
            if not content or content.lower() in existing:
                continue
            session.add(
                UserMemory(user_id=user_id, content=content, kind=m.get("kind", "fact"))
            )
            existing.add(content.lower())
            added += 1
        if added:
            await session.commit()
            logger.info("remembered %d new flat fact(s) for %s", added, user_id)


async def remember(user_id: str, user_text: str, assistant_text: str) -> None:
    """Extract + store durable facts from one exchange. Fire-and-forget.

    Single-pass extraction into `user_memories` — the one long-term store.
    """
    try:
        await _flat_remember(user_id, user_text, assistant_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("remember failed: %s", exc)

