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

## Milestone 5 — Personal profile & memory  ⟵ BUILT (pending user verify)
- [x] `models/user_profile.py` — structured profile (name, tz, locale, rolling sentiment, prefs, `daily_checkin_hour`, `last_refresh_on`)
- [x] `models/user_memory.py` — free-form extracted facts (text + kind); `models/mood_log.py` — per-turn sentiment signal
- [x] `services/memory/profile_service.py` — profile ensure/read + sentiment/refresh writes (pure DB, hot-path safe)
- [x] `services/memory/memory_service.py` — `recall()` (fast DB read) / `remember()` (LLM fact-extraction, fire-and-forget, dedup)
- [x] **DEVIATION from plan:** lightweight Postgres-backed memory instead of **Mem0 + pgvector** — same `remember()`/`recall()` contract, swappable later. Rationale: Mem0+embeddings is the third-party complexity the user rejected for research, and neither Groq nor OpenRouter cleanly offer embeddings. No new heavy deps.
- [x] `services/memory/sentiment_service.py` — `note_sentiment()` (cheap per-turn SLM, fire-and-forget) + `rollup_sentiment()` (periodic batch LLM summary → profile)
- [x] **Daily research refresh** (`services/research/refresh_service.py`) — the "6am, has registration opened?" check: re-researches watched tasks (`requires_research=True`, open) and applies concrete updates (new `due_at`, fresh findings in `context`). Scheduler fires hourly, self-gates to the user's local `daily_checkin_hour` (per-user time, restart-safe via `last_refresh_on`).
- [x] `services/scheduler/scheduler_service.py` — added `daily_task_refresh` (cron, hourly self-gating) + `sentiment_rollup` (interval) jobs alongside the due-sweep
- [x] `main.py` — profile read into session context at connect; recalled facts + profile injected into the escalated LLM context (so research/create_task see them); fire-and-forget `remember()` + `note_sentiment()` post-turn
- [x] `core/config.py` — `MEMORY_RECALL_LIMIT`, `DAILY_CHECKIN_HOUR`, `SENTIMENT_ROLLUP_SECONDS`, `SENTIMENT_WINDOW_DAYS`; `requirements.txt` — `tzdata` (zoneinfo on Windows)
- [ ] **USER VERIFY:** state a preference ("I prefer concise answers") → new session → it's recalled and shapes a relevant turn; set a `requires_research` task + `daily_checkin_hour` to the current hour → daily job logs a refresh
- [ ] **STOP — done**

## Milestone 6 — Auth (Google sign-in via Auth0)  ⟵ BUILT (pending user verify)
Real multi-user auth, replacing the hardcoded `"local-user"` placeholder everywhere.

**Switched from Supabase Auth to Auth0 mid-milestone** (user call — Supabase's Google provider requires standing up your own Google Cloud OAuth client; Auth0 ships dev keys for its Google social connection, less setup). Auth0 React SDK (Authorization Code + PKCE) handles login entirely client-side; this backend only ever verifies the resulting **ID token** against Auth0's public JWKS (RS256) — never talks to Auth0's token endpoint or Google directly. No separate Auth0 API resource was configured, so we verify the ID token (audience = the Auth0 client id) rather than a resource-server access token — sufficient for "who is this," which is all this app needs.

- [x] `models/user.py` — added `email` column (comment updated: `id` is now the Auth0 `sub` claim, e.g. `"google-oauth2|123"`, not a Supabase UUID); `db/session.py` — `ALTER TABLE users ADD COLUMN IF NOT EXISTS email`
- [x] `core/config.py` — `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET` (secret stored for future use, unused by the PKCE+JWKS verification flow itself); `requirements.txt` — `pyjwt[crypto]>=2.8.0` (crypto extra needed for RS256)
- [x] `services/auth/auth_service.py` — `decode_token()` (`jwt.PyJWKClient` fetches/caches Auth0's JWKS, verifies RS256 + `aud`/`iss`), `profile_fields()` (pulls `email`/`name` off claims), `get_current_user_id()` (FastAPI `Depends`, reads `Authorization: Bearer`), `authenticate_websocket()` (reads `?token=` since browsers can't set custom WS headers; closes with code 1008 *before* `accept()` on missing/invalid token)
- [x] `services/tasks/task_service.py` — `ensure_user()` now accepts `email`; added `list_user_ids()` for the scheduler's per-user loops
- [x] `services/memory/profile_service.py` — `ensure_profile()` accepts `display_name`/`email`, seeded from claims on first contact
- [x] `main.py` — `/ws/voice` verifies the token before `ws.accept()`, uses the verified `sub` as `user_id` (no more hardcoded placeholder); profile load seeds name/email from claims; `GET /tasks` and `GET /reminders/due` switched from a trusted `user_id` query param to `Depends(get_current_user_id)`
- [x] **Multi-user scheduler:** `services/scheduler/scheduler_service.py` — all three jobs (`check_due_tasks`, `daily_task_refresh`, `sentiment_rollup`) now loop over every row in `users` via `list_user_ids()` instead of one hardcoded user
- [x] Frontend: `@auth0/auth0-react` added (replaces `@supabase/supabase-js`, removed); `main.jsx` wraps the app in `Auth0Provider`; `Login.jsx` calls `loginWithRedirect({ authorizationParams: { connection: "google-oauth2" } })` to skip straight to Google; `App.jsx` reads `isAuthenticated`/`getIdTokenClaims()`/`logout()`, gates the launch screen behind sign-in, passes the raw ID token down; `VoiceChat.jsx` unchanged (already took a generic `accessToken` prop) — appends `?token=` to the WS URL and sends `Authorization: Bearer` on `GET /tasks`
- [x] `.env`/`.env.example` (backend + `client/`) — `AUTH0_DOMAIN`/`AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET` and `VITE_AUTH0_DOMAIN`/`VITE_AUTH0_CLIENT_ID`; Postgres (`DATABASE_URL`) stays on Supabase — only auth moved, not the DB
- [x] Verified: `npm run build` clean on the client; `python -c "import main"` clean in the real venv (no import-time errors)
- [ ] **Not done by this backend (Auth0 dashboard config, outside the codebase):** add your dev URL (e.g. `http://localhost:5173`) to **Allowed Callback URLs**, **Allowed Logout URLs**, and **Allowed Web Origins** on the Auth0 application; confirm the Google social connection is enabled (Authentication → Social)
- [ ] **USER VERIFY:** click "Sign in with Google" → completes OAuth → lands on the launch screen → "Start Conversation" connects the WS successfully → `/tasks` loads without a 401
- [ ] **STOP — get approval**
