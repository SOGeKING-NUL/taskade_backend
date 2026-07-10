"""
Memory service — semantic/factual long-term memory, backed by mem0.

This replaces the previous hand-rolled stack (flat UserMemory keyword recall +
temporal knowledge graph + reflections). mem0 gives us what that sprawl was
approximating but couldn't do well: vector (semantic) search, automatic
extraction of durable facts from a conversation, and de-duplication — all in a
library call instead of ~700 lines of custom extraction we had to maintain.

What lives WHERE now (the memory taxonomy):
  • Semantic / factual / procedural LTM  → HERE (mem0, vector store in Postgres).
    Stable facts about the user: "based in Pune", "studies best in the morning",
    "prefers concise answers".
  • Episodic memory (events the user experienced) → the `tasks` table. A done
    task IS an episode ("ran the Tuffman half marathon"); a dated task is an
    upcoming one. No separate episodic store is needed.
  • Working memory (STM) → the bounded conversation history in the prompt.

Backend: Gemini for both extraction LLM and embeddings (free tier), pgvector in
the same Supabase Postgres. Kept behind this thin module so the rest of the app
calls `remember()` / `recall()` and never sees mem0 directly.
"""

import asyncio
import logging
from urllib.parse import urlparse, unquote

from core.config import settings

logger = logging.getLogger(__name__)

# ── mem0 config (Gemini LLM + Gemini embedder + pgvector) ─────────────────
def _pg_conn_string() -> str:
    """Build a libpq KEYWORD-format DSN from the app's async DATABASE_URL.

    We deliberately do NOT hand mem0 individual host/user/password fields: its
    pgvector backend then assembles a `postgresql://{user}:{password}@{host}` URI,
    and any '@' (or other reserved char) in the password — ours has one — corrupts
    that URI so the host is misparsed. Keyword format quotes each value instead, so
    special characters are safe. mem0 uses a supplied connection_string as-is."""
    u = urlparse(settings.DATABASE_URL.replace("+asyncpg", ""))

    def q(v: str) -> str:  # libpq keyword-value quoting
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"

    return " ".join([
        f"host={u.hostname}",
        f"port={u.port or 5432}",
        f"dbname={(u.path or '/postgres').lstrip('/') or 'postgres'}",
        f"user={q(unquote(u.username or ''))}",
        f"password={q(unquote(u.password or ''))}",
    ])


def _mem0_config() -> dict:
    key = settings.GOOGLE_API_KEY
    return {
        "llm": {
            # Memory extraction runs on OpenRouter (gpt-4o-mini), NOT the hot-path
            # Gemini brain: (1) it matches the project's provider split (brain=Gemini,
            # all background work=OpenRouter), and (2) Gemini's free tier throws
            # frequent 503s that were silently dropping memory writes. mem0's "openai"
            # provider auto-routes to OpenRouter when OPENROUTER_API_KEY is in the
            # environment (core.config's load_dotenv puts it there).
            "provider": "openai",
            "config": {"model": settings.OPENROUTER_LLM_MODEL, "temperature": 0.1},
        },
        "embedder": {
            "provider": "gemini",
            # gemini-embedding-001 forced to 768 dims (its default is 3072). The
            # older text-embedding-004 404s for newer Gemini projects. 768 must
            # match `embedding_model_dims` in the vector_store below.
            "config": {
                "model": "models/gemini-embedding-001",
                "api_key": key,
                "embedding_dims": 768,
            },
        },
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "connection_string": _pg_conn_string(),
                # NOT "user_memories" — that's the legacy flat-facts table
                # (models/user_memory.py), which has no vector column. mem0 needs
                # its own table or it queries a vector op on the wrong schema.
                "collection_name": "mem0_memories",
                "embedding_model_dims": 768,
            },
        },
    }


# Lazily-built singleton. Construction creates clients + the pgvector collection
# table (one-time blocking DB work), so we build it in a worker thread the first
# time and reuse it. `AsyncMemory` construction is sync; its add/search are async.
_memory = None
_build_lock = asyncio.Lock()


async def _get_memory():
    global _memory
    if _memory is None:
        async with _build_lock:
            if _memory is None:
                from mem0 import AsyncMemory
                _memory = await asyncio.to_thread(AsyncMemory.from_config, _mem0_config())
                logger.info("mem0 memory store initialised (gemini + pgvector)")
    return _memory


def _memories(raw) -> list[str]:
    """mem0 returns either {'results': [...]} or a bare list depending on version."""
    items = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    return [m.get("memory", "") for m in items if isinstance(m, dict) and m.get("memory")]


async def recall(user_id: str, query: str = "", limit: int = 5) -> list[str]:
    """Return the most relevant durable facts for the user.

    With a `query` (the current utterance) → semantic search: the facts most
    related to what the user is talking about right now. Without one → the most
    recent facts (e.g. for building a greeting). Fast, hot-path safe; swallows
    its own errors so a memory hiccup never breaks a turn.
    """
    try:
        mem = await _get_memory()
        filters = {"user_id": user_id}
        if query:
            raw = await mem.search(query=query, filters=filters, top_k=limit)
        else:
            raw = await mem.get_all(filters=filters, top_k=limit)
        return _memories(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory recall failed: %s", exc)
        return []


async def remember(user_id: str, user_text: str, assistant_text: str) -> None:
    """Extract + store durable facts from one exchange. Fire-and-forget.

    mem0 decides what (if anything) is worth keeping and de-duplicates against
    what's already stored — so unlike the old flat store, repeating "I'm in Pune"
    doesn't pile up duplicate rows.
    """
    if not (user_text or assistant_text):
        return
    try:
        mem = await _get_memory()
        messages = [
            {"role": "user", "content": user_text or ""},
            {"role": "assistant", "content": assistant_text or ""},
        ]
        await mem.add(messages, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory remember failed: %s", exc)
