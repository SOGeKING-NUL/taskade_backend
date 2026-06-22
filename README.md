# Voice-First Personal Assistant

A real-time, full-duplex **voice** assistant that goes beyond chat: it understands
intent, calls tools, manages a durable task tree, runs live web research, delivers
reminders, and remembers the user across sessions — all governed by one streaming
voice loop over a single authenticated WebSocket.

- **Backend deep-dive:** [`system_explanation.md`](system_explanation.md)
- **Frontend deep-dive:** [`frontend_explanation.md`](frontend_explanation.md)
- **Milestone status:** [`CHECKLIST.md`](CHECKLIST.md)

---

## What it does

- 🎙️ **Always-on voice** — speak naturally; no push-to-talk. Instant barge-in.
- ⚡ **Two-tier brain** — a fast SLM answers chat/factual turns directly; it
  escalates to a tool-calling LLM only when the turn needs an action.
- ✅ **Task tree** — single-step reminders and multi-step goals with hierarchy
  (`parent`) and sequencing (`depends_on`); finishing a prerequisite auto-unblocks
  the next step.
- 🔎 **Live research** — the model web-searches itself to fill in dates/details
  before creating a task.
- ⏰ **Reminders** — a scheduler detects what's due; a REST endpoint delivers it.
- 🧠 **Memory** — a structured profile plus free-form recalled facts and sentiment,
  learned in the background without slowing the conversation.
- 🔐 **Auth0 Google sign-in** with persistent sessions.

---

## Provider Stack

| Role | Provider / Model |
|---|---|
| Speech-to-Text | Deepgram **Nova-3** (server-side endpointing) |
| Fast path (SLM) | Groq **`llama-3.1-8b-instant`** |
| Tool/research (LLM) | OpenRouter **`openai/gpt-4o-mini`** + web search |
| Text-to-Speech | Sarvam **Bulbul** (streaming WAV) |
| Auth | **Auth0** (Google social login, PKCE) |
| Database | **Postgres** (Supabase, IPv4 session pooler) |

Groq and OpenRouter are both OpenAI-compatible, so a single `openai` SDK serves
both. The latency-critical "just answer" path stays on Groq direct; the routing
hop only lands on the already-slow tool path.

---

## Real-time pipeline

```
[ Mic ] → PCM-16 frames → WebSocket → Deepgram STT (endpointing)
   → Groq SLM (fast)  ──answers──────────────────────────────┐
        └─escalate─→ OpenRouter LLM ⇄ Tools ⇄ Postgres        │
                          (create_task / query / research)    │
   → sentence chunker → Sarvam TTS → PCM-16 → [ Speaker ]  ◄──┘
```

The text producer and the TTS consumer run concurrently (`asyncio.gather` + a
sentence queue), so the user hears sentence 1 while sentence 2 is still being
generated. Sentence boundaries (`.!?।;:`) drive the handoff.

---

## Running it

### Backend
```bash
# from the project root, using the project venv
./venv/Scripts/python.exe -m pip install -r requirements.txt
./venv/Scripts/python.exe main.py        # serves on :8000
```
Requires a `.env` (see `.env.example`) with the Deepgram / Groq / OpenRouter /
Sarvam / Auth0 keys and a `DATABASE_URL` pointing at the Supabase **session
pooler** (the direct `db.<ref>.supabase.co` host is IPv6-only and will time out on
most networks).

### Frontend
```bash
cd client
npm install
npm run dev                               # serves on :5173
```
Requires `client/.env` with `VITE_WS_URL`, `VITE_API_URL`, `VITE_AUTH0_DOMAIN`,
`VITE_AUTH0_CLIENT_ID`. The dev origin must be allow-listed in the Auth0 dashboard
(Callback / Logout / Web Origins).

---

## API surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| WS | `/ws/voice?token=<jwt>` | Auth0 ID token | The voice session |
| GET | `/health` | — | Liveness |
| GET | `/tasks` | `Bearer <jwt>` | List the user's tasks |
| GET | `/reminders/due` | `Bearer <jwt>` | Deliver + mark due reminders |
