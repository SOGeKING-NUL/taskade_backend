> **Completed.** Every change proposed below has shipped: the two-layer leak
> detector lives in `services/ai/slm.py`, time-bound tasks work via `due_at`/
> `window_start`/`window_end`, `GET /debug/scheduler` exists, and the `metadata` WS
> message + `MetadataCard.jsx` are live. Kept as a record of the reasoning; see
> `system_explanation.md` §14 and `architecture_overview.md` for how these work
> today (some details below, like the specific tool list and `escalate_to_assistant`,
> predate the later tool-surface simplification in `db_revamp.md`).

# Future Implementation Plan — Tool Reliability, Time-Bound Tasks & Richer Metadata

## Context

A live bug exposed a tool call leaking into the spoken/displayed response as literal
text instead of executing:

```
<function(update_task_status){"task": "Register for New Balance City Series 10K Marathon", "new_status": "done"}</function>
```

Investigating this surfaced two related gaps — short-term, clock-time-specific tasks
("dinner with mother Saturday at 3pm") aren't yet steered the way long-term tasks are,
and stored metadata (links, amounts) isn't surfaced well when asked for.

This revision incorporates feedback on the first draft:
- **The leak fix must not be a single regex for one observed syntax.** We don't know
  what shape a future malformed call will take, so the detector needs to generalize
  across syntaxes, not just catch the one we happened to see.
- **The scheduler/research-retry system needs visibility.** There's no way today to see
  what APScheduler has queued, or which tasks are waiting on a research retry and when.
- **Metadata should not be spoken at all** — it should appear as a distinct, clickable
  UI element separate from the conversational text, because the production app will be
  voice-first and the chat transcript won't exist as a UI surface later. Spoken text and
  structured/clickable data need to be different channels now, while we still have a
  transcript to lean on as a crutch.
- Tool specificity (parent/child, CRUD) was confirmed adequate as drafted — unchanged.

## Investigation summary

- **The leaked call is a known class of issue, not a fluke** — Groq's own docs flag
  tool-use reliability as imperfect for general-purpose models, and there are open
  reports questioning `llama-3.3-70b-versatile`'s tool-calling consistency specifically.
  It will recur, and **not necessarily in the same shape twice** — so matching one exact
  tag syntax (e.g. `<function(...)>`) is fragile by construction.
- **A genuinely robust detector doesn't need to know the wrapper syntax at all.** Every
  tool name is a small, closed, known set we already own —
  `services/tools/dispatcher.py`'s `TOOL_REGISTRY` (`create_task`, `query_tasks`,
  `update_task_status`, `research`, `update_profile`). A real spoken sentence would
  never naturally contain one of these exact snake_case identifiers as a literal
  substring — so checking for **their presence**, not the surrounding punctuation, is
  syntax-agnostic: it doesn't matter whether the model wraps the leak in `<function(...)>`,
  raw JSON, a markdown fence, or anything else, because the thing INSIDE the wrapper
  (the tool name) is what we actually control and can enumerate.
- **Root cause, precisely:** `services/ai/slm.py`'s `run_conversation()` only escalates
  to the `OpenRouterLLM` fallback when Groq raises an `openai.APIError`. When the model
  instead streams the malformed call as ordinary `delta.content` — no error, just
  wrong-shaped text — `tool_calls` stays empty, the loop's `if not tool_calls:` branch
  treats it as a genuine final answer, and the text gets spoken. The fallback path
  already exists and works (`main.py` escalates to `OpenRouterLLM` on a `{"type":
  "fallback"}` event) — it's just never triggered for this failure mode.
- **APScheduler jobs are already introspectable for free.** `scheduler_service.py`'s
  `_scheduler.get_jobs()` returns `Job` objects exposing `.id`, `.next_run_time`, and
  `.trigger` — no extra bookkeeping needed to know when `check_due_tasks`,
  `daily_task_refresh`, and `daily_reflection_sweep` will next fire.
- **Per-task research-retry state is already on the task, just not surfaced.** After
  the B/C/D work, a structured-research task's `context.research_intent` carries
  `query`, `success_condition`, `retry_interval_days`, and `next_attempt_at`; its last
  outcome lives in `context.research_refresh`. Nothing currently reads this across all
  of a user's tasks and presents it — it's there, just invisible.
- **The WS protocol already drops the data needed for a metadata card.**
  `services/tools/research_tools.py`'s `research()` returns full `links`/`findings`,
  and `query_tasks` returns full `to_brief()` (now including `context`) — but
  `main.py`'s `tool.result` WS message only forwards `{name, ok, summary}` to the
  client (confirmed in both `main.py` and `VoiceChat.jsx`'s `case "tool.result"`
  handler). The richer payload is computed server-side and then thrown away before it
  reaches the frontend — it just needs a wire to travel on.

## Proposed changes

### 1. Fix: tool calls leaking as literal text (highest priority)

**File:** `services/ai/slm.py`

Two-layer detector — a high-confidence check (primary signal) and a generic fallback
check (catch-all), so detection isn't tied to one observed leak shape:

```python
from services.tools.dispatcher import TOOL_REGISTRY

_TOOL_NAME_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in TOOL_REGISTRY) + r")\b"
)
_STRUCTURAL_LEAK_PATTERN = re.compile(r'[<{]\s*["\']?\w+["\']?\s*[:(]')

def _looks_like_leaked_tool_call(text: str) -> bool:
    return bool(_TOOL_NAME_PATTERN.search(text) or _STRUCTURAL_LEAK_PATTERN.search(text))
```

- **Primary signal:** the text contains one of our own registered tool names verbatim
  (built FROM `TOOL_REGISTRY` so it can't drift out of sync as tools are added/removed).
  This is the high-confidence check — it doesn't care what syntax wraps it.
- **Secondary signal:** generic structural-anomaly pattern (an opening brace/angle
  bracket immediately followed by what looks like a key or function name) — a broader
  net for a malformed call that, for some reason, doesn't include an exact tool name.
- On either match: don't speak it, yield `{"type": "fallback", "reason":
  "malformed_tool_call_text"}` — reusing the exact `OpenRouterLLM` escalation path
  `main.py` already has wired up. No changes needed in `main.py`.
- **Observability:** log the raw leaked text (`logger.warning`) whenever this fires, so
  recurring shapes can be reviewed later and folded into the structural pattern if a
  new one shows up repeatedly — detection improves over time without needing to
  predict every future shape up front.

### 2. Time-bound / short-term task support

**Files:** `services/tools/schemas.py`, `services/ai/slm.py`

- Strengthen `create_task`'s `due_at` description: when the user gives a clock time
  ("9pm," "3pm," "noon"), it MUST be encoded in `due_at`'s time component — never
  collapse to midnight/date-only.
- Add `window_start` / `window_end` as optional `create_task` parameters (the columns
  already exist on `Task`, just not wired to the tool) for looser phrasing ("Saturday
  evening") where a single timestamp would overstate precision the user didn't give.
- Add an explicit system-prompt rule: resolve relative time phrases ("tonight," "this
  evening," "in an hour") against the current date/time the same way the prompt already
  resolves relative *dates* — don't let time-of-day silently drop.
- No scheduler changes needed here — `get_due_reminders` already compares full
  timestamps; once `due_at` carries real time-of-day, same-day reminders become
  time-accurate automatically.

### 3. Scheduler & research-retry visibility (new)

The actual ask: a way to *see* what's scheduled — pending research retries, when the
next sweep runs — instead of only inferring it from logs.

**Backend — `services/scheduler/scheduler_service.py`:**
- Add `get_job_status() -> list[dict]`: maps `_scheduler.get_jobs()` to
  `{id, next_run_time, trigger}` for the three registered jobs
  (`check_due_tasks`, `daily_task_refresh`, `daily_reflection_sweep`).

**Backend — `services/tasks/task_service.py`:**
- Add `get_research_schedule(session, user_id) -> list[dict]`: every task with
  `requires_research=True`, returning title, the structured `research_intent`
  (`query`, `success_condition`, `retry_interval_days`, `next_attempt_at`), and the
  last outcome from `context.research_refresh` if present — sorted by `next_attempt_at`
  (soonest first; legacy tasks with no structured intent sort last, since they poll on
  every sweep).

**Backend — `main.py`:** new authenticated endpoint
`GET /debug/scheduler` returning `{jobs: [...], research_schedule: [...]}` —
global job state plus the calling user's own watched-task schedule (kept consistent
with every other endpoint's per-user scoping; no cross-user data exposure).

**Frontend — new `SchedulerPanel.jsx`,** alongside the existing `TasksPanel`/
`ToolActivity` panels in `VoiceChat.jsx`: a simple table — job name + next run time at
the top, then a list of watched tasks with their next retry time, query, and last
outcome. Polled on an interval (e.g. every 30s) while visible, not on the hot path.
Plain table is sufficient for a debug surface — no charts/visualizations needed.

### 4. Metadata: a separate structured channel, not spoken recitation

Rather than asking the model to recite links/amounts out loud (unreliable, and the
wrong shape for a voice-first product where there will be no transcript to fall back
on), separate the two channels structurally:

- **Spoken text stays short and pointer-like** — "Here are the details" / "I've pulled
  up the registration info" — the existing "never read out IDs/URLs/raw data" prompt
  rule is **kept as-is**, not loosened, since it was already pushing in the right
  direction for this.
- **A new WS message carries the structured data directly from the tool result** —
  deterministic, not dependent on the model choosing to recite anything correctly.
  `main.py`'s tool-result handling forwards a richer payload when one is present:
  ```python
  # after the existing tool.result send:
  metadata = _extract_presentable_metadata(result)  # links / amounts / dates, if any
  if metadata:
      await _send_json(ws, {"type": "metadata", "tool": ev["name"], "data": metadata})
  ```
  `_extract_presentable_metadata()` pulls `links`/`findings` from a `research` result,
  or `context.research`/`due_at` from a `create_task`/`query_tasks` result — all data
  that already exists in the tool's return dict today; it's just never been forwarded.
- **Frontend renders it as its own card**, separate from the chat bubble — clickable
  links, a small structured block (date, amount, link), persistent in the panel rather
  than only existing as a line of spoken text. This is the durable substitute for "read
  the transcript" once the product is voice-only.

## Files touched

- `services/ai/slm.py` — two-layer leak detector; time-of-day prompt rule.
- `services/tools/schemas.py` — `due_at` description strengthened; `window_start`/
  `window_end` added to `create_task`.
- `services/scheduler/scheduler_service.py` — `get_job_status()`.
- `services/tasks/task_service.py` — `get_research_schedule()`.
- `main.py` — `GET /debug/scheduler`; `metadata` WS message alongside `tool.result`.
- `client/src/components/SchedulerPanel.jsx` (new) — scheduler/research visibility UI.
- `client/src/components/VoiceChat.jsx` — handle the new `metadata` message type;
  render `SchedulerPanel`.

No database migration needed — everything reads data that already exists.

## Verification

1. **Leak fix:** repeatedly trigger the "mark this task done" phrasing that produced
   the leak (probabilistic, not a deterministic repro). Confirm the task's status
   actually changes and the spoken response is a natural confirmation, never literal
   tool-call-shaped text. Separately, sanity-check the detector doesn't false-positive
   on a normal sentence that happens to contain a brace or colon.
2. **Time-bound tasks:** create "remind me to call Kunal at 9pm tonight" and "dinner
   Saturday at 3pm"; confirm `due_at` carries the correct time via `/tasks`.
3. **Scheduler visibility:** open the new panel, confirm it shows the three jobs' next
   run times and any tasks currently waiting on a research retry, matching what's
   actually in the DB (`context.research_intent.next_attempt_at`).
4. **Metadata card:** ask about a task with stored research/links; confirm the spoken
   response stays short, and a separate clickable card appears with the link/details —
   not recited in speech, not buried in the chat bubble's text.
5. **Regression:** confirm the prior B/C/D fixes (deterministic task pre-retrieval,
   `research_intent` scheduler retries) still pass — these changes are additive.
