# Recent Changes — What We Built and Why

This is a narrative changelog: for each change, the problem that triggered it, what we
did, and why it mattered. For how the system works *today*, see `system_explanation.md`
(backend) and `frontend_explanation.md` (frontend). This file explains the *sequence* —
the bugs hit, the decisions made, and the reasoning behind each one.

---

## 1. Auth: Supabase Auth → Auth0

**Problem:** Supabase's Google sign-in required standing up our own Google Cloud OAuth
client — too much setup friction for what should've been a quick win. Auth0 has
built-in dev keys for Google sign-in, so we switched.

**What changed:** Frontend now uses `@auth0/auth0-react` (Authorization Code + PKCE),
calling `loginWithRedirect({ authorizationParams: { connection: "google-oauth2" } })` to
skip Auth0's hosted page and go straight to Google. The backend never talks to Auth0's
token endpoint or Google directly — its only job is verifying the **ID token** Auth0
already issued, via RS256 + JWKS (`services/auth/auth_service.py`). The verified `sub`
claim (e.g. `google-oauth2|123...`) is the user id used everywhere — same identity, same
data, regardless of which provider authenticated it.

**Follow-up fixes:**
- *Callback URL mismatch* — pure Auth0 dashboard config (Allowed Callback/Logout URLs,
  Allowed Web Origins), no code involved.
- *Session not persisting across reloads* — the default Auth0 SDK config relies on an
  iframe-based silent-auth check that most browsers now block via third-party cookie
  restrictions. Fixed by switching to `cacheLocation="localstorage"` +
  `useRefreshTokens` + the `offline_access` scope, so renewal uses a stored refresh
  token instead of the blocked iframe flow. The actual session *duration* is a dashboard
  setting (Refresh Token Rotation → Absolute/Inactivity Expiration), not code.

---

## 2. Database connectivity: direct connection → session pooler

**Problem:** Backend crashed on startup — `TimeoutError` connecting to
`db.<ref>.supabase.co:5432`. DNS lookup confirmed that host has **only an IPv6 (AAAA)
record**, no IPv4 — Supabase's direct connections are IPv6-only unless you pay for an
IPv4 add-on, and the network here couldn't route IPv6 to it.

**Fix:** Switched `DATABASE_URL` to Supabase's **session pooler** endpoint
(`aws-1-ap-southeast-2.pooler.supabase.com:5432`, username `postgres.<project-ref>`),
which is IPv4-reachable. Verified with a direct `asyncpg` connection test before
declaring it fixed.

---

## 3. TTS bug: multi-sentence answers cut off after the first sentence

**Problem:** Multi-sentence answers only spoke their first sentence; the rest played
**at the start of the next turn**, before that turn's real answer — and the backlog
compounded over a long conversation.

**Root cause:** `services/voice/tts.py`'s `SarvamTTS.stream_tts()` holds one **persistent**
WebSocket to Sarvam, reused across turns. Sarvam sends a `final`/`completion` event
**per sentence** (not per turn), but the code treated the *first* such event as "the
whole turn is done" and returned — leaving later sentences' audio sitting unread on the
still-open socket, where it got drained at the start of the *next* `stream_tts()` call.

**Fix:** Track `sentences_sent` vs. `sentences_completed`; only end the generator once
*both* "no more sentences will be sent" and "every sent sentence has completed" are
true. A per-sentence completion event now just increments a counter and continues,
instead of ending the whole stream.

---

## 4. Backend freeze after ~6 rapid turns in under a minute

**Problem:** After roughly 6 conversational turns within a minute, the backend went
completely unresponsive — Deepgram stopped delivering transcripts, requiring a restart.

**Diagnosis (ruled out vs. confirmed):**
- *Not* a context-window issue — 6 turns is ~12 short messages against a 128k window.
- *Confirmed*: every turn fired 3+ LLM calls (foreground SLM + 2 background
  learn/sentiment calls), tripping free-tier per-minute rate limits under rapid use.
- *Confirmed, the actual "permanent" cause*: `services/voice/stt_deepgram.py`'s receive
  loop **died silently with no reconnect** on any socket drop (an idle timeout during
  long TTS playback, a network blip). Once dead, `send_audio()` silently dropped every
  subsequent chunk — the backend went permanently deaf with zero error surfaced.

**Fixes:**
- STT auto-reconnect with backoff, plus real Deepgram `KeepAlive` messages so the socket
  survives silent gaps during playback (`stt_deepgram.py`).
- Batched background learning — memory extraction now runs once every 3 turns instead
  of every turn, cutting per-minute API volume.
- Explicit `timeout`/`max_retries` on every `AsyncOpenAI` client so a hung call fails
  fast instead of stalling a turn.
- DB connection pool explicitly sized with a finite `pool_timeout` (`db/session.py`).
- Conversation history capped at 20 messages to prevent unbounded growth.

---

## 5. Architecture collapse: 3-layer (SLM→escalate→LLM) → 2-layer (SLM as sole brain)

**Problem (user-reported):** Asking "when's the JLPT and when can I register" produced
**two answers back to back** — a stale, confident-but-wrong date from the fast SLM,
*then* the real researched date. The 3-layer design (Groq SLM routes → escalates to an
OpenRouter LLM that does the actual tool-calling) meant the SLM's text streamed to
speech immediately, before anyone knew whether it was about to escalate.

**Why we didn't just patch around it:** The user pointed out the deeper issue — two
models inferring over the same input, with only a one-line intent string bridging them
(though full `conversation_history` *was* actually shared, contrary to first
appearances). The fix that actually addressed *latency, call volume, and clarity*
simultaneously was collapsing to one model.

**What changed:**
- `services/ai/llm.py`'s `OpenRouterLLM` orchestrator was retired as the primary brain
  (kept only as a fallback escape hatch — see §10).
- `services/ai/slm.py`'s `GroqSLM` became the **sole** reasoning + tool-calling model,
  bumped to **`llama-3.3-70b-versatile`** (the 8B model wasn't reliable enough at
  knowing when to call tools vs. just answer).
- **Structural fix for the double-answer bug**, stronger than the original patch: text
  produced in a tool-calling round is *never* spoken — only a round that makes **no**
  tool calls yields speakable text. So even if the model both answers-from-memory and
  calls a tool in one round, the stale answer is discarded, not just deferred.
- The "let me look that up" filler (previously blocked by the TTS bug in §3) was wired
  in once that bug was fixed — it speaks while `research` runs in the background.

---

## 6. Task-creation consent: prompt rule → structural code gate

**Problem:** The assistant created tasks the user never asked for — asked to research
something, it created a task anyway. A prompt instruction alone wasn't reliable enough.

**Fix:** `create_task`'s tool schema now requires `user_confirmed: bool` (required
field). `services/tools/task_tools.py` refuses to write the task unless
`user_confirmed=true` was explicitly asserted — returning "ask the user to confirm
first" instead. This can't be silently skipped by the model forgetting a prompt rule,
because the code itself enforces it.

---

## 7. Location personalization (auto-captured, not asked every time)

**Problem:** The user wanted location-aware research ("marathons near me") without
having to state their city every conversation.

**What changed:** Frontend asks the browser for geolocation **once**
(`navigator.geolocation`), reverse-geocodes it to "Delhi, India" (keyless service, no
API key), and persists it via `POST /profile/location`. On every future load, `GET
/profile` is checked first — if a location is already stored, the browser is **never
asked again**. The stored location is injected into the SLM's prompt every turn via
`_memory_context`, so "find me a marathon" is automatically scoped to the user's city.

---

## 8. Sentiment/mood: removed from the hot path, replaced with on-demand engagement

**Problem (user's framing):** Mood tracking added a per-turn classification LLM call
and a "recent mood" prompt injection to the **fast conversation path**, where it didn't
belong — sentiment only matters at moments like app-open or a notification, not mid-task.

**Fix:** Deleted the `mood_log` table and `sentiment_service.py` entirely, along with the
scheduler's rollup job and the per-turn injection. Replaced with
`services/engagement/engagement_service.py`: an **on-demand** `generate_greeting()`
that reasons over the profile + recent memories + upcoming tasks in a single LLM call,
generated only when `GET /engagement/greeting` is actually called (app-open / future
notifications) — never on the conversation path.

---

## 9. Temporal Knowledge Graph (entities, edges, reflections, mood signals)

**Problem:** Flat `UserMemory` facts have no relationships and no concept of time — the
system couldn't tell "studying for JLPT" relates to "December deadline," and couldn't
distinguish normal silence on a 6-month goal from concerning silence on a 2-day one.

**What was built** (`models/entity.py`, `models/reflection.py`,
`services/memory/graph_service.py`, `services/memory/reflection_service.py`):
- **Entities + temporal edges** — subject→predicate→object triples with `valid_from`/
  `valid_until`. Contradicting facts don't get deleted, they get the old edge's
  `valid_until` stamped — preserving *when* something changed.
- **Differential decay** — edges carry a `horizon_scale` (micro/short/medium/long) so a
  long-term goal isn't flagged as abandoned just because it wasn't mentioned today.
- **Reflections** — a multi-scale background sweep (daily/weekly/monthly) that diffs the
  graph and generates short, factual "state-delta" observations ("JLPT mentions stopped
  10 days ago; exam is in 3 weeks") — explicitly **not** psychological trait inference.
- **Mood signals** — per-entity valence tags that adjust the assistant's *tone*, never
  spoken as a claim about the user's emotions.

This runs **alongside** the flat `UserMemory` extraction (not yet replacing it) until
the graph path is trusted; `_memory_context` and `engagement_service` both read from it.

---

## 10. Tool-routing & retrieval hardening

**Problem (user-reported):** Asked for a previously-created task's registration link,
the assistant **re-ran web research** instead of checking the task it already had
stored — wasteful, and revealed two distinct gaps.

**Fixes, deliberately made as deterministic as possible (not relying on model judgment):**
- **`Task.to_brief()` now includes `context`** — previously `query_tasks` couldn't see
  stored research/links at all, so even a correct tool choice would've failed to
  retrieve the answer.
- **Deterministic pre-retrieval** (`task_service.find_relevant_tasks()`): every turn,
  the raw transcript is word-matched against active task titles — a fixed DB lookup,
  not an LLM decision — and any hit's stored research/links are injected into the
  prompt *before* the model picks a tool. This removes the routing failure mode
  structurally rather than hoping the model reads the rules correctly.
- **`research_intent`-driven scheduler retries** (`services/research/refresh_service.py`):
  tasks created with a structured `{query, success_condition, retry_interval_days}`
  intent are now polled using their **exact** stored query, evaluated against their
  **exact** stated success condition, and retried on a real schedule (`next_attempt_at`)
  instead of re-polling — and burning a web-search call — every single day regardless
  of the requested interval. On success, polling stops (`requires_research` cleared)
  and the finding is stored in the same canonical slot the pre-retrieval step reads.

This also surfaced (and confirmed already-fixed) that the consent gate (§6) and the
2-layer collapse (§5) both *exceeded* what an earlier planning document
(`implementation_plan.md`) had proposed for these areas.

---

## 11. Time-bound tasks + a tool-call-leak detector that generalizes

**Problem:** A tool call leaked into the spoken/displayed response as literal text
(`<function(update_task_status){...}</function>`) instead of executing. The first
instinct — match that exact tag syntax — was rejected: we don't know what shape a
*future* malformed call will take, so a one-off regex is fragile by construction.

**Fix (`services/ai/slm.py`):** a two-layer detector that doesn't depend on the wrapper
syntax at all. Primary signal: the text contains one of our own registered tool names
verbatim — built live from `TOOL_REGISTRY`, so it can't drift — because a real spoken
sentence would never naturally contain a snake_case function identifier, regardless of
what punctuation wraps it. Secondary signal: a generic structural pattern (brace/angle
bracket immediately followed by a key-like token) as a catch-all. Either match routes
into the `OpenRouterLLM` escalation path that already existed for API-level failures.

**Same pass, time-bound tasks:** the system had only been exercised on long-term,
date-only tasks. Added: the system prompt now gives the model the current *time*, not
just the date, and an explicit rule to preserve a stated clock time ("9pm tonight") in
`due_at` rather than collapsing to midnight; `window_start`/`window_end` were wired
through to `create_task` for loose phrasing ("Saturday evening") where a single
timestamp would overstate precision.

---

## 12. Scheduler visibility + a separate channel for task metadata

**Problem:** Two distinct asks. First — there was no way to *see* what the research-retry
scheduler had queued; everything was inferred from logs. Second — the user wanted
task metadata (links, fees, dates) to stop being something the model recites in speech
at all, since the production app will be voice-only with no transcript to fall back on —
metadata needed its own durable, clickable UI surface.

**What was built:**
- `services/scheduler/scheduler_service.py`'s `get_job_status()` + `task_service.py`'s
  `get_research_schedule()`, exposed via `GET /debug/scheduler`, and a new
  `SchedulerPanel.jsx` — shows the three APScheduler jobs' next-run times plus every
  task currently waiting on a research retry, its query, and its last outcome.
- A new WS message type, `metadata`, carrying structured data (links/dates/findings)
  straight from a tool's result — deterministically, not dependent on the model
  choosing to recite anything correctly — rendered by a new `MetadataCard.jsx` (the
  "blue info box") as its own card, separate from the chat bubble. The existing "never
  read out IDs/URLs/raw data" prompt rule was kept exactly as-is rather than loosened —
  it was already right for a voice-first product; the data just needed its own channel.

**Follow-up fix (narrowing the trigger):** the card initially appeared for *any* tool
result carrying presentable data — every `research` call, every `create_task`, any
`query_tasks` listing — which was far too broad; the user wanted it to appear *only*
when explicitly asking about one specific, already-tracked task. Fixed by threading the
tool's original call `args` through the event pipeline (previously only the *result*
was visible, not what was asked for) and gating `_extract_presentable_metadata()` in
`main.py` to fire in exactly one case: `query_tasks` called with
`scope == "specific_task"`. `research`, `create_task`, and `update_task` are now
unconditionally suppressed regardless of what they return.

---

## 13. `update_task`, retroactive dependency linking, and research auto-attach

**Problem (user-reported, three bugs from one real conversation):** Asked to set up a
marathon registration reminder, then add a link and fee to it, then add a dependent
"participate in the race" reminder — the result was a **duplicate** registration task
(no edit), **no dependency link** between the two tasks, and `context`/`requires_research`
left **null** despite the assistant having just spoken the fee and link aloud.

**Root causes:**
- No tool existed to *amend* an existing task — only `update_task_status` (status-only).
  Asked to "add the link to that reminder," the model had nothing to call but
  `create_task` again, producing a duplicate instead of an edit.
- The dependency-linking prompt rule only covered creating both tasks *together*; it
  said nothing about linking to a prerequisite that already existed from an earlier turn.
- The model spoke the research findings but never copied them into the `create_task`
  call's `research_summary`/`source_links` args — a data-plumbing gap, not a judgment one.

**Fixes:**
- New `update_task` tool (`task_service.py`, `task_tools.py`, `schemas.py`,
  `dispatcher.py`) — patches title/description/`due_at`/window/parent/dependency, and
  **merges** new research findings into existing `context.research` rather than
  overwriting, so amending a task twice doesn't lose what was already stored.
- Prompt rule: link `depends_on_task`/`parent_task` to something already tracked from an
  earlier turn (using the existing relevant-tasks context block from §10), not just
  when creating both tasks in one breath.
- **Deterministic auto-attach** (`run_tool_loop`, see §14): the most recent successful
  `research` result in the turn is threaded into the next `create_task`/`update_task`
  call automatically if the model didn't supply its own — removing reliance on the model
  remembering to copy data between its own tool calls.

Verified end-to-end on an isolated in-memory DB: one register task (no duplicate),
correct merge into `context.research`, correct `depends_on_id` link with `blocked`
status, and correct auto-unblock to `pending` once the prerequisite is marked done.

---

## 14. Fallback model was silently running a stale, weaker prompt

**Problem (user-reported, found while debugging §13):** Logs showed Groq's tool-call
formatting failing (`"Failed to call a function"` — a documented Groq/Llama-3.3
reliability gap) and correctly escalating to the `OpenRouterLLM` fallback. But
`services/ai/llm.py` had its **own**, separately-maintained system prompt — written
before this session's work on time-of-day resolution, `update_task`, retroactive
dependency linking, and research auto-attach. Every time Groq hiccups, the conversation
silently dropped to that much weaker, outdated instruction set with none of those fixes
— invisible until specifically investigated, because the fallback still "worked," just worse.

**Fix:** extracted the entire tool-calling loop — prompt, leak detection, auto-attach —
into one shared `run_tool_loop(client, model, messages, session_context)` in `slm.py`,
parameterized by client/model. Both `GroqSLM` and `OpenRouterLLM` now delegate to it;
`llm.py` no longer carries its own copy of anything. There is now structurally only one
place this logic can exist, so the primary and fallback paths can't drift apart again.
Verified: `GroqSLM().system_prompt == OpenRouterLLM().system_prompt` is `True`.

---

## 15. Brain swap: Groq SLM → Gemini; OpenRouter demoted to research-only

**Problem (user-reported + diagnosed):** Two compounding issues with Groq's
`llama-3.3-70b-versatile` as the brain:
1. **The "freeze after ~6 turns" was Groq rate limiting, not a context-window bug.**
   Every turn ships ~8K tokens (system prompt + 6 tool declarations + up to 20 history
   messages + the memory block). Groq's free tier is ~6K tokens/min, so a normal voice
   conversation 429s within a handful of turns; the STT socket then times out waiting on
   a reply and the pipeline appears to "freeze." No amount of prompt-trimming fixes a
   per-minute quota that low.
2. **Tool-call formatting was unreliable** — a documented Groq/Llama weakness. It would
   sometimes emit a malformed `<function(update_task_status){...}</function>` as ordinary
   text, which got spoken aloud. §11/§14 built a leak detector + a whole OpenRouter
   fallback brain *just to paper over this*.

**Decision (why Gemini, why via the OpenAI-compatible endpoint, why not LangChain):**
- We evaluated Gemini Flash vs. staying on Groq. Groq's raw TTFT is faster on paper, but
  that advantage is ~150ms — smaller than one STT round-trip, and irrelevant next to the
  multi-second dead-air a 429-retry causes. Gemini 2.5 Flash-Lite gives **250K TPM + a 1M
  context window on the free tier** (vs. Groq's 6K TPM), which makes the freeze class of
  bug structurally impossible, plus **reliable native function calling**, which removes
  the reason the leak-detector/fallback existed.
- Gemini speaks the OpenAI wire format at
  `https://generativelanguage.googleapis.com/v1beta/openai/`, so we kept the existing
  `AsyncOpenAI` client and the shared `run_tool_loop` **verbatim** — only the client
  config (key + base_url + model) changed. LangChain was considered and rejected: it
  would add a heavy dependency and force a rewrite of the tool loop into its abstractions,
  for zero functional gain over a one-line base_url swap. (Ladder: reuse what's here.)

**What changed:**
- **New `services/ai/brain.py`** (`GeminiBrain`) replaces `services/ai/slm.py`. It owns the
  system prompt + `run_tool_loop`. `slm.py` and `llm.py` were **deleted** — the whole
  OpenRouter *fallback brain* is gone, since Gemini's native tool calling is reliable.
- **The `{"type":"fallback"}` escalation path in `main.py` was removed.** A hard Gemini API
  error now surfaces as a graceful spoken line instead of switching models. The leak
  detector was reduced from the two-layer (TOOL_REGISTRY-coupled) version to **one cheap
  structural regex** whose only job is to never speak a raw JSON/tool-call blob aloud.
- **OpenRouter is demoted to research-only.** It is no longer a brain; it backs the
  `research` tool exclusively. Per the request, the research model is now
  **`openai/gpt-4o-mini:online`** (GPT-4o-mini + live web search via OpenRouter's `:online`
  suffix). Background memory extraction and reflections also moved off Groq onto
  `openai/gpt-4o-mini` (they used `settings.SLM_MODEL`, which no longer exists).
- **System prompt rewritten to 2026 voice-agent standards** (researched before editing):
  labeled sections (Role / Now / Scope / Research / Creating tasks / Editing vs creating /
  Multi-step plans / Answering about tasks / Style), short action-oriented rules, and
  voice-first formatting. Every behavioral fix from §3–§14 (consent gate, update-vs-create,
  retroactive dependency linking, clock-time preservation, research-before-answering) was
  preserved — the prose is tighter, ~40% shorter, not weaker. Standard: describe tools by
  what they do, avoid leaking resource-ID slugs into spoken output.
- **Config/env cleanup:** removed `GROQ_API_KEY`, `GROQ_BASE_URL`, `SLM_MODEL`, the legacy
  unused `LLM_MODEL`/`LLM_SYSTEM_PROMPT`, and the dead `ESCALATE_TOOL` (a leftover from the
  retired 3-layer design, §5). Added `GEMINI_BASE_URL` / `GEMINI_MODEL`.

**Caveat flagged to user:** the provided Gemini key (`AQ.Ab8RN6…`) is **not** the usual
AI Studio format (those start with `AIza`); it looks like an OAuth/ephemeral credential and
may not authenticate against this endpoint. The `.env` keeps a valid-format `AIza…` key as a
commented fallback to switch to if Gemini returns 401.

**Verification (user runs live tests):** app imports clean; no dangling references to the
removed symbols (`grep` clean for `GroqSLM`/`OpenRouterLLM`/`SLM_MODEL`/`GROQ_`/`ESCALATE_TOOL`).
Live checks to run: a long (10+ turn) conversation no longer freezes; "check that task off"
performs a real status update with a spoken confirmation (no literal tool-call text); a
research query streams the "let me look that up" filler then returns current facts.
