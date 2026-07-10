# mem0-implementation Branch

This branch introduces a **semantic memory layer** (mem0) and replaces the Groq SLM with a single Gemini brain. It is a significant architectural upgrade from `main`.

## Key Differences from Main

### Brain Architecture
- **main**: Groq SLM (6K TPM free tier) → escalates to OpenRouter LLM fallback on tool-call failures
- **mem0-implementation**: Single **Gemini `gemini-flash-latest`** brain (250K TPM free tier) with native function calling; no fallback escalation

### Memory Stack
- **main**: Legacy flat UserMemory table (no semantic search)
- **mem0-implementation**: mem0 v2 semantic LTM layer
  - **Extraction LLM**: OpenRouter `gpt-4o-mini` (reliable, background-only)
  - **Embedder**: Gemini `gemini-embedding-001` @ 768 dims
  - **Backend**: pgvector in Supabase (`mem0_memories` collection)
  - **Auto-dedup & extraction**: Facts are extracted and deduplicated automatically

### Research Output
- **main**: Raw markdown headers/bullets
- **mem0-implementation**: Plain speech-normalized prose; location context deterministically threaded to avoid Sydney/Michigan misses

### Reliability Fixes
- Hard 25-second backstop per brain step (prevents "stuck thinking" on Gemini free-tier 503s)
- TTS markdown stripper (fixes ticking noise from malformed sentence fragments)
- Gemini thought_signature handling (fixes 400 errors on tool-call follow-up rounds)

## Files Changed
- `services/ai/brain.py` — new (replaces `slm.py` + `llm.py`)
- `services/memory/memory_service.py` — new mem0 wrapper
- `services/research/research_service.py` — location context, markdown normalization
- `main.py` — hard timeout loop, TTS markdown stripper
- `requirements.txt` — added mem0ai, google-genai, pgvector, psycopg2-binary
- `db/session.py` — pgvector extension initialization

## Testing Status
- ✅ mem0 end-to-end verified (add + semantic recall against real Supabase)
- ✅ Brain function-call round-trip verified
- ✅ Thought_signature handling verified
- 🧪 Full integration test: run "Set up a meeting with [name] at 3PM today" in the live app

## Merging to Main
**Not ready yet.** This branch should be tested in a live session first:
1. Confirm memory persistence (tell it a fact, restart, verify recall)
2. Confirm timed task creation ("dinner at 9pm tonight")
3. Confirm all research queries use user's location (no Sydney/Michigan misses)

Once live-tested, merge to main and retire the old graph/reflection system (currently dormant).

---

For details on the design, see `architecture_overview.md` and `.claude/projects/.../memory/memory-layer.md`.
