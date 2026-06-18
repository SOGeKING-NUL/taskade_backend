# Build Checklist — Voice-First Personal Assistant Backend

Build order is strict: each milestone is a prerequisite for the next. **Stop for approval after every milestone.**

Full architecture rationale lives in the plan file (`~/.claude/plans/please-go-thoruhg-the-sharded-deer.md`).

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

---

## Milestone 1 — Tool-calling foundation  ⟵ AWAITING APPROVAL
Fast SLM (Groq) answers normal turns; escalates to OpenRouter LLM for tool calls. Task tools stubbed in-memory (no DB yet).

- [x] Add deps: `openai` SDK (replaces `google-generativeai`); `requirements.txt` updated; installed in venv
- [x] `config.py`: added `GROQ_API_KEY`, `SLM_MODEL`, `GROQ_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_LLM_MODEL`, `OPENROUTER_BASE_URL`
- [x] `.env.example`: new keys + placeholders + default models
- [x] `services/slm.py` — `GroqSLM`: streaming fast path + `escalate_to_assistant`, yields tagged events
- [x] `services/llm.py` — `OpenRouterLLM`: full tool registry, streaming tool-call accumulation, multi-round tool loop (`run_conversation`)
- [x] `services/tools/schemas.py` — `escalate_to_assistant` + `create_task`/`query_tasks`/`update_task_status` declarations
- [x] `services/tools/task_tools.py` — in-memory stub implementations (tested)
- [x] `services/tools/dispatcher.py` — `execute_tool()` with structured error handling (tested: not-found, unknown-tool)
- [x] `services/tools/__init__.py` — `TOOL_REGISTRY`, `get_tool_declarations()`
- [x] `main.py` — SLM-first routing, escalation loop, filler-on-escalate, `session_context`, new WS events (`escalated`, `tool.start`, `tool.result`)
- [x] Frontend: `VoiceChat.jsx` handles new events; `components/ToolActivity.jsx` activity log; `vite build` passes
- [x] Verified: py_compile clean, imports OK, tool dispatch logic exercised end-to-end (create/query/update/error paths)
- [ ] **USER VERIFY with live keys:** "what's 2+2" = instant; "create a task to test" = escalation + tool chip + spoken confirmation
- [ ] **STOP — get approval**

## Milestone 2 — Task tree data model (Postgres)  ⟵ AWAITING APPROVAL
- [x] Add deps: SQLAlchemy 2.0 async, asyncpg; `requirements.txt` (Alembic deferred to M5 — see note)
- [x] `db.py` — async engine/session factory + `init_db()`; `config.py` `DATABASE_URL`
- [x] `models/{base,user,task}.py` — `tasks` adjacency-list schema (parent_id + depends_on_id, status, windows, context JSONB/JSON)
- [x] `services/task_service.py` — CRUD, find/fuzzy-match, tree, dependency-unblock on completion
- [~] Alembic scaffold — **deferred to M5**: using `create_all()` on startup so there's no migration step now; Alembic gets a baseline when the schema first changes (profiles/pgvector)
- [x] Swap `services/tools/task_tools.py` stubs → real DB calls (same return contract)
- [x] `main.py` — `lifespan` runs `init_db()`; new `GET /tasks` endpoint
- [x] Frontend: `components/TasksPanel.jsx` reads `GET /tasks`, refreshes after each tool result
- [x] Verified (SQLite harness): create single + milestone tree, dependency auto-block, complete→unblock, error paths, **persistence across process restart**
- [x] **USER VERIFY with Postgres:** Supabase connected (fixed `@`-in-password URL encoding); tasks persist
- [x] **APPROVED** — "it works well, move on to next milestone"

## Milestone 3 — Research tool (OpenRouter native web search)  ⟵ APPROVED
Research = a plain high-intelligence **Claude** LLM call with `:online` (Anthropic native web search) + a research system prompt. No Perplexity Sonar, no Tavily/Exa.
- [x] `services/research/research_service.py` — Claude `:online` call → normalized contract (summary, links, source_count)
- [x] `services/tools/research_tools.py` — `research` tool adapter (short summary for UI + full findings for the LLM)
- [x] Register `research` in dispatcher + schema
- [x] `create_task` persists `research_summary` + `source_links` into the task's `context` JSONB
- [x] `config.py`/`.env.example`: `OPENROUTER_RESEARCH_MODEL` (default `anthropic/claude-sonnet-4.6:online`)
- [x] **APPROVED** — confirmed working live
- [x] Bugfix: research/LLM/SLM system prompts now inject today's real date per-call (was defaulting to training-data-salient past dates, e.g. "JLPT December 2023" instead of the real upcoming date)
- [x] Bugfix: `create_task` no longer auto-fires after `research` — LLM only creates a task when the user explicitly asked for one or just confirmed a yes/no offer; otherwise it answers and asks first
- [x] Intelligence: LLM now told to split prerequisite + event into two tasks (e.g. "register" → "take exam") wired via `depends_on_task`
- [x] SLM escalation prompt now also escalates bare confirmations ("yes"/"no") replying to the LLM's own pending offer, not just explicit task language

> Restructure note: flattened back out of `app/` to the project root per explicit instruction (core/db/models/services/utils live at root, no wrapper package). Dead `api/` stub package and stray root `__init__.py` removed.

## Milestone 4 — Reminders & query loop  ⟵ IN PROGRESS
- [x] Reactive: `query_tasks` already answers "what's on today/this month" (free from M2) — no changes needed
- [x] `models/task.py` — `last_reminded_at` column; `db/session.py` patches it onto existing tables via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (Alembic still deferred to M5)
- [x] `services/tasks/task_service.py` — `get_due_reminders()` / `mark_reminded()` for idempotent due-task lookup; `consume_due_reminders()` fetches+marks in one transaction (single source of the spoken-summary string)
- [x] **Decoupled delivery from the websocket** (per user): removed `_announce_due_reminders()` and its auto-call from `voice_websocket()`. Delivery is now `GET /reminders/due?user_id=` — calling it IS the delivery (atomic fetch + mark), invoked "at necessary times" by an external caller, never by the WS itself
- [x] **Detection split out** into `services/scheduler/scheduler_service.py` — `AsyncIOScheduler` runs one recurring `check_due_tasks()` sweep every `REMINDER_SWEEP_SECONDS` (default 60); read-only, logs only, never marks reminded. Started/stopped from `main.py` `lifespan()`. In-memory job store (nothing to persist beyond `due_at`/`last_reminded_at` in Postgres)
- [x] `core/config.py` — `REMINDER_SWEEP_SECONDS`; `requirements.txt` — `apscheduler>=3.10.0`
- [x] Why two halves never call each other: if both marked tasks reminded, whichever ran first would swallow the reminder before the other delivered it — so "mark reminded" lives only in the pull-based API; the sweep is a heartbeat/future hook for a real outbound channel (mobile/WebRTC)
- [ ] **USER VERIFY:** backdate a task's `due_at` in DB → within `REMINDER_SWEEP_SECONDS` server log shows detection (no DB change) → `GET /reminders/due` returns it + message → call again → empty (consumed)
- [ ] **STOP — get approval**

## Milestone 5 — Personal profile & memory
- [ ] `models/user_profile.py` — structured profile (name, tz, sentiment, prefs)
- [ ] `services/memory_service.py` — Mem0 + pgvector `remember()`/`recall()`
- [ ] `services/sentiment_service.py` — periodic batch sentiment rollup
- [ ] `main.py` — profile read into session context; fire-and-forget memory write post-turn
- [ ] Tools pull `recall()` context before research/create_task
- [ ] Verify: state a preference → new session → recall surfaces it during a relevant task
- [ ] **STOP — done**
