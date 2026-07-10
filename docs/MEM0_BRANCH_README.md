# mem0-implementation branch

This branch replaces the memory and brain stack that `main` currently runs. Where `main` uses a Groq SLM that escalates to an OpenRouter LLM when tool-calling glitches, this branch runs a single Gemini model as the brain end to end, reached through an OpenAI-compatible endpoint, with no separate escalation step. Memory is the bigger change: `main` stores whatever it remembers about you as flat rows with no real search — this branch replaces that with mem0, which extracts facts in the background through OpenRouter, embeds them with Gemini's embedding model, and stores them in pgvector so recall is an actual semantic search rather than exact-match lookups.

A few smaller things got fixed along the way that are worth calling out. Research output used to come back as raw markdown (headers, bullet points) that read badly when spoken aloud — it's now plain prose, and location is threaded through deterministically so a research query actually uses where the user is instead of drifting to whatever city happened to come up in the search results. There's also a hard 25-second ceiling on each step the brain takes, so a slow or failing Gemini call can't leave the conversation stuck in a "thinking" state with no response ever coming back — and a fix for a Gemini-specific bug where tool calls were failing on the follow-up round because a required `thought_signature` field wasn't being passed back.

## What changed, file by file

`services/ai/brain.py` is new and replaces both `slm.py` and `llm.py` — it's the single Gemini brain. `services/memory/memory_service.py` is rewritten as a thin wrapper around mem0. `services/research/research_service.py` picked up location-awareness and the markdown-to-prose fix. `main.py` got the timeout backstop and a text-to-speech markdown stripper. `requirements.txt` picked up `mem0ai`, `google-genai`, `pgvector`, and `psycopg2-binary`, and `db/session.py` now initializes the pgvector extension on startup.

## Testing status

The mem0 layer has been verified end to end against the real Supabase database — writing a memory and recalling it semantically both work. The brain's tool-calling round-trip and the thought_signature fix are also verified. What hasn't been tested yet is a full live session end to end: creating a timed task by voice, restarting and confirming memory persists, and confirming research consistently uses the right location across a real conversation.

## Merging to main

Not yet — this branch needs a live test pass first, covering the three items above. Once that's confirmed, this can merge into main and the old flat-memory system (along with the dormant graph/reflection code sitting unused in this branch) can be removed.
