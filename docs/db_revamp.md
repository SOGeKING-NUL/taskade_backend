# Database & Memory/Tools Revamp

A record of the July 2026 revamp that simplified the persistence layer, the
memory system, and the tool/task surface — what changed, **why each decision was
made**, and what it optimized. For how the system works today after this change,
read this alongside `system_explanation.md` / `architecture_overview.md` (which
predate it and still describe the removed layers).

## Goals

1. **Simplify the memory system** — it had grown into five overlapping stores.
2. **Bring tools & the task tree to industry-standard shape** — one tool had 16
   parameters, a known reliability hazard for tool-calling models.
3. **Revamp the schema to match current needs** — drop what nothing uses.

Driving constraint throughout: *simpler and more reliable beats more capable.*
The product is a latency-critical, tool-heavy voice assistant, so every per-turn
read and every tool parameter is a cost.

---

## Schema: before → after

| Table | Before | After | Note |
|---|---|---|---|
| `users` | ✓ | ✓ | unchanged |
| `user_profiles` | ✓ | ✓ | unchanged |
| `user_memories` | ✓ | ✓ | **now the single long-term store** |
| `tasks` | ✓ | ✓ | dropped `depends_on_id`, `requires_research` |
| `reminders` | ✓ | ✓ | unchanged (clean delivery ledger) |
| `device_tokens` | ✓ | ✓ at the time | **later dropped entirely** (2026-07-19) — the web client polls instead of using push, and no mobile client existed to register a token; see `recent_changes.md` §18 |
| `entities` | ✓ | ✗ | dropped — knowledge graph |
| `entity_edges` | ✓ | ✗ | dropped — knowledge graph |
| `reflections` | ✓ | ✗ | dropped — reflection layer |
| `mood_signals` | ✓ | ✗ | dropped — reflection layer |

**10 tables → 6.**

---

## Key decisions & rationale

### 1. Memory store: minimal custom on Postgres — NOT mem0, LangMem, or LangGraph

**Decision:** keep `user_memories` as the one durable "what we know about the
user" store; single-pass LLM extraction; no vector DB yet.

**Why not LangMem / LangGraph:** the codebase is a deliberately framework-free
async `AsyncOpenAI` tool loop. LangMem is built for LangGraph; adopting it drags
in a graph runtime that fights our token-level streaming + barge-in real-time
loop and adds hot-path overhead — for an agent that is a *single bounded tool
loop*, not a multi-node workflow. LangMem's own releases are also stale (PyPI
0.0.30, Oct 2025). We verified we use no LangGraph/LangChain anywhere before
deciding.

**Why not mem0 (for now):** mem0 is framework-agnostic and would fit, but it adds
a dependency + its own opinions for a fact set that is small (dozens of facts per
user). Per Mem0's own benchmark, a *compact extracted memory injected wholesale*
beats full-context — which a slim custom store already achieves. mem0 remains a
documented future swap if the fact set outgrows wholesale injection; the
`memory_service` interface (`recall` / `remember`) was kept backend-agnostic so
that swap touches one file.

**Optimization:** the per-turn memory read dropped from **5 sources → 3**
(profile + facts + deterministic task-match), and extraction went from **two LLM
passes (flat + graph) → one**.

### 2. Drop the knowledge-graph + reflection layer entirely

**Decision:** delete `entities`, `entity_edges`, `reflections`, `mood_signals`
and their services.

**Why:** these were three additional representations of the same "what we know"
signal, explicitly the lowest-trust / most-experimental layers, and they were
queried on the **hot path every turn** (active-edges + entity-name-map +
reflections + mood). High code-maintenance cost, low incremental value over the
flat store. Removing them is the single biggest simplification.

### 3. Background consolidation, not inline per-turn extraction

**Decision:** long-term facts are extracted off the response path
(fire-and-forget), and the scheduler owns durable-memory maintenance.

**Why:** the 2026 industry consensus is that memory consolidation belongs on an
asynchronous background path, never in the user-facing turn — extraction latency
must not tax the conversation. This also matched the existing APScheduler
investment. (The scheduler's per-turn *reflection sweep* was removed; the memory
write path stays fire-and-forget.)

### 4. Tasks: aggressive simplification

**Decision:** remove the dependency/blocking chain (`depends_on_id`) and the
scheduled research-retry machine (`requires_research`, `research_intent`,
`refresh_service`, `daily_task_refresh`).

**Why:** both were elegant but rarely-exercised complexity for a voice MVP. The
dependency-unblock demo (`register` → unblocks `book hotel`) and the "6am,
did registration open?" re-poll added tool parameters, prompt text, a background
job, and a whole service — for behavior most turns never touch. The task tree
(`parent_id` hierarchy) is kept; it's cheap and useful.

### 5. Tool surface: fewer tools, smaller schemas

**Decision:** 6 tools → 5; `create_task` 16 params → 11; folded
`update_task_status` into `update_task`; removed the vestigial
`escalate_to_assistant`.

**Why:** a 16-parameter tool is a documented reliability hazard — it correlates
with the malformed/leaked tool-call bug, and it's worst on the fast models we
need for fluidity. Smaller, single-intent tools are the industry-standard shape
and directly reduce leaked-call risk. `escalate_to_assistant` was dead code from
the retired 3-layer (SLM→escalate→LLM) architecture.

**Kept:** the `research` *tool* (on-demand web search) — only the scheduled
*re-poll* of research was removed.

### 6. Migrations: idempotent DDL in `init_db`, not Alembic

**Decision:** extend the existing `create_all` + `ALTER … IF (NOT) EXISTS`
pattern with `DROP TABLE IF EXISTS … CASCADE` and `ALTER TABLE … DROP COLUMN IF
EXISTS`.

**Why:** keeps the zero-migration-tooling simplicity the project already relies
on; every statement is idempotent, so startup is safe to re-run. Alembic remains
the future option if the schema starts evolving in ways `create_all` can't
express. **The drops execute on the next app startup** (`init_db()` in the
lifespan).

### 7. Provider: Groq is the active brain

**Decision:** populate `GROQ_API_KEY` so `slm.py` / `engagement_service.py` run
on Groq; memory extraction stays on OpenRouter.

**Why:** the code reads `GROQ_*`; the `.env` had drifted to `GEMINI_*`, so the
app couldn't start (empty key → `AsyncOpenAI` throws at import). Groq also has the
lowest time-to-first-token, which serves the fluidity goal. Note: `SLM_MODEL`
defaults to `llama-3.1-8b-instant`, which is weak at tool-calling — bump to a
tool-capable model (e.g. `llama-3.3-70b-versatile`) via `.env` if tool calls get
unreliable.

---

## What changed, by milestone

- **M1 — Memory read/write collapse.** `_memory_context` reduced to profile +
  facts + task-match; `remember()` single-pass; removed the reflection sweep;
  engagement greeting composes from profile + facts + tasks.
- **M2 — Delete graph/reflection.** Removed `graph_service.py`,
  `reflection_service.py`, `models/entity.py`, `models/reflection.py`; `init_db`
  drops the four tables.
- **M3 — Tasks & tools.** Dropped `depends_on_id` + `requires_research`; slimmed
  `create_task`; folded `update_task_status` into `update_task`; removed
  `escalate_to_assistant`; deleted `refresh_service.py` +
  `get_research_schedule` + `daily_task_refresh`; simplified `/debug/scheduler`.
- **M4 — This document.**

## Optimizations, summarized

| Axis | Before | After |
|---|---|---|
| Tables | 10 | 6 |
| Per-turn memory reads | 5–6 sources | 3 |
| Memory extraction passes | 2 (flat + graph) | 1 |
| Scheduler jobs | 5 | 3 |
| Action tools | 6 (+1 vestigial) | 5 |
| `create_task` params | 16 | 11 |
| Deleted service/model files | — | 5 |

## Deferred / future options

- **pgvector semantic recall** — add only when wholesale injection of the compact
  profile outgrows the context budget (YAGNI today).
- **mem0** — the documented alternative long-term backend; the `memory_service`
  contract is kept swap-friendly.
- **Alembic** — if the schema needs migrations `create_all` can't express.
- **Session-end / scheduled consolidation** — extraction could move from
  every-3rd-turn to session end or a nightly job; the store already supports it.

## Testing notes

- `import main` and `compileall` pass clean after every milestone.
- The table/column drops apply on next app startup; feature testing (voice loop,
  task CRUD, tool-calling) is done by the developer against a running instance.
