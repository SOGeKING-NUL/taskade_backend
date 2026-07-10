# Architecture Upgrade: Continuity-Aware Temporal Knowledge Graph

This document details the architectural pivot from a "flat fact" memory system to a **Temporal Knowledge Graph**, alongside necessary safeguards for the autonomous task and research engines.

## Why We Are Making These Changes (The Problem)

The current `user_memories` system treats every extracted fact (e.g., "Studying for JLPT," "Prefers morning sessions") as an isolated string with no relationship to other facts and no concept of time. 

This creates three critical failures in the assistant's ability to act like a real human:
1. **No Contextual Relationships:** The system cannot understand how facts interact (e.g., that studying JLPT is related to a December timeline). 
2. **Trait Inference vs. State Delta:** Because facts are flat, generating a greeting or "mood" insight often results in robotic, psychoanalytical trait inferences (e.g., "You seem anxious about deadlines"). A human assistant doesn't psychoanalyze; they notice *changes in state* (e.g., "You haven't mentioned JLPT in two weeks, still on track?").
3. **Recency Bias & Silence:** The current system treats all "silence" equally. If a user doesn't mention a short-term driver's test, it's relevant. If they don't mention a marathon that is 6 months away, it's normal. The current system cannot differentiate between these temporal horizons.

Additionally, the task creation engine is currently too eager—creating tasks without explicit user consent—and research tasks lack a structured "intent", causing them to fail blindly without a retry mechanism.

## The Solution: Semantic Graph Search & Temporal Edges

To solve this, we are introducing a **Semantic Graph Search** mechanism built on a **Temporal Knowledge Graph**. 

**Why it is important:**
Instead of storing isolated facts, we will store **Nodes** (Entities like "JLPT" or "Rachel") connected by **Edges** (Relationships like "is planning"). Crucially, every edge has a `valid_from` and `valid_until` timestamp. 
By introducing this Semantic Graph, the system can perform **State-Delta Detection**. Instead of guessing the user's mood, the system queries the graph to see *what has changed* since last week. It allows the assistant to gracefully handle goals of varying temporal scales (daily vs. monthly) by applying **Differential Decay** (e.g., knowing not to drop a long-term goal just because it hasn't been mentioned in a few days).

## User Review Required

> [!IMPORTANT]
> Please review the expanded theoretical approach and the concrete implementation steps below. Once approved, I will begin the code modifications.

---

## Proposed Changes (The "How")

### 1. Database Schema: Temporal Knowledge Graph
We will introduce new relational tables in PostgreSQL that act as a Graph database.

#### [NEW] `models/entity.py`
- **Why:** To give the system concrete "Nodes" to attach information to, rather than free-text blobs.
- **How:** Create `Entity` (`entities` table: `id`, `user_id`, `type`, `name`, `summary`) and `EntityEdge` (`entity_edges` table: `id`, `user_id`, `source_id`, `target_id`, `relation`, `valid_from`, `valid_until`, `horizon_scale`). The `horizon_scale` (short/medium/long) dictates how fast this goal decays.

#### [NEW] `models/reflection.py`
- **Why:** To store the high-level insights generated from the graph deltas.
- **How:** Create `Reflection` (`reflections` table: `id`, `user_id`, `content`) and `MoodSignal` (`mood_signals` table: `id`, `entity_id`, `valence`). Valence acts as a tone-modifier (e.g., `trending_negative`), not a fact spoken out loud.

#### [MODIFY] `db/session.py`
- Add initialization for the new tables (`entities`, `entity_edges`, `reflections`, `mood_signals`).

---

### 2. Memory Extraction: Entity Resolution & Differential Decay

#### [MODIFY] `services/memory/memory_service.py`
- **How:** Update `remember()` to perform **Entity Resolution**. We will prompt the async LLM to output subject-predicate-object triples. It will merge new entity mentions with existing Nodes.
- **How:** Implement **Temporal Invalidation**. When a new edge contradicts an old one (e.g., "I pushed the driver's test to next Friday"), we do *not* delete the old edge. We set `valid_until = today` on the old edge and create a new one. This preserves the history of the change.

---

### 3. Reflections: State-Delta Detection

#### [NEW] `services/memory/reflection_service.py`
- **How:** Create a multi-scale background job (Daily, Weekly, Monthly sweeps).
- **How:** This job will diff the knowledge graph to find what changed (the "delta"). It will apply **Differential Decay**, meaning it checks up on short-term goals daily, but only checks long-term goals monthly, generating "Check-in" reflections when a goal goes suspiciously quiet.

#### [MODIFY] `services/engagement/engagement_service.py`
- **How:** Update `generate_greeting()` to ingest these generated *Reflections* and *Mood Signals* rather than raw facts, outputting highly contextual, continuity-aware greetings.

---

### 4. Fix: Autonomous Task Creation & User Consent
The system currently creates tasks without asking, leading to "ghost tasks."

#### [MODIFY] `services/ai/slm.py`
- **How:** Inject a strict behavioral rule into the `system_prompt` for the Groq SLM: "If research fails or a dependency is noted, report the failure to the user and ASK if they want a reminder created. NEVER invoke `create_task` without explicit user confirmation."

---

### 5. Fix: Structured Research Intents & Retries
Current background research tasks fail silently because they don't know exactly what to query or when to try again.

#### [MODIFY] `models/task.py`
- **How:** Document the expected schema for the `context` JSONB column to include `research_intent` (containing `query`, `success_condition`, `on_failure`).

#### [MODIFY] `services/tasks/task_service.py`
- **How:** Update the `create_task` tool schema so the SLM is forced to define the specific search `query` and `success_condition` when `requires_research` is True.

#### [MODIFY] `services/scheduler/scheduler_service.py`
- **How:** Update the polling logic. When the scheduler picks up a research task, it uses `research_intent.query`. If the result fails the `success_condition` (e.g., the registration link isn't live yet), it follows the `on_failure` directive to bump the `due_at` date forward by a specific interval (e.g., 24 hours) to automatically retry.

### 6. Fix: Graceful SLM to LLM Fallback
The SLM often fails to construct strictly formatted JSON tool calls when user requests are inherently vague (e.g. throwing `openai.APIError`).

#### [MODIFY] `services/ai/slm.py`
- **How:** Catch `openai.APIError` natively within the streaming completion loop. If the error implies a tool-formatting failure, cleanly abort the run and yield a `fallback` event instead of crashing.

#### [MODIFY] `main.py`
- **How:** Update the `_produce_text` orchestrator to listen for the `fallback` event. When detected, immediately escalate the entire conversational context to the smarter `OpenRouterLLM` (which has better reasoning to either parse the vague intent or ask the user a clarifying question).

## Verification Plan

### Automated Tests
- Validate that `memory_service.remember` caps `valid_until` instead of deleting when resolving a contradiction.
- Validate that the scheduler correctly reschedules a task (bumps `due_at`) when a `success_condition` fails.

### Manual Verification
- **Graph Test:** Tell the assistant "I am studying for the JLPT." Then, "I decided to drop the JLPT." Verify the graph edge is invalidated temporally, not deleted.
- **Consent Test:** Ask "When does the JLPT open?" Verify the assistant fails gracefully and *asks* for permission to create a polling task.
- **Retry Test:** Create a task looking for a known dead URL. Verify the scheduler attempts it, evaluates the failure condition, and bumps the deadline to the future autonomously.
