# Agentic Memory Systems: Theory and Architecture

This is a fantastic question. The AI memory space has evolved incredibly fast over the last two years, borrowing heavily from cognitive psychology and graph mathematics. Let's break down all the jargon, theory, and the exact approach we discussed so you have a rock-solid understanding of what we're building and why.

Here is your crash course on the theory and terminology behind **Agentic Memory Systems**.

---

## 1. The Core Vocabulary: Graphs and Time

### Knowledge Graph
In traditional databases, data is stored in flat tables (rows and columns). In a **Knowledge Graph**, data is stored as a web of **Nodes** (Entities) and **Edges** (Relationships). 
*   **Node (Entity)**: A noun. "User", "Japanese", "JLPT N5", "Sarah".
*   **Edge (Relationship)**: A verb or connection. "Is studying", "Is friends with", "Is planning".
*   Example: If you extract "I am studying Japanese", the graph looks like: `[User] --(is studying)--> [Japanese]`.

### Temporal Edges
A standard knowledge graph only knows what is true *right now*. A **Temporal Knowledge Graph** adds the dimension of time. Every edge (relationship) gets a `valid_from` and `valid_until` timestamp. 
*   **Why it matters:** When information changes (e.g., the user stops studying Japanese), the system does not delete the old edge. It simply caps `valid_until` to today. This allows the AI to query the past and understand *when* things changed. If the user says "I want to get back into what I was studying last year," the system can literally query the graph for "edges valid during 2025" and remember it was Japanese. 

### Entity Resolution
This is a classic Natural Language Processing (NLP) task. When a user says "my sister" on Tuesday and "Sarah" on Thursday, the system must figure out they are the same person. **Entity Resolution** is the LLM-driven step of matching a newly mentioned entity to an existing Node in the graph, rather than creating duplicates or brand new ones.

---

## 2. Cognitive Psychology & AI Memory Theory

Modern AI memory frameworks heavily borrow from how human brains work.

### Episodic vs. Semantic Memory
*   **Episodic Memory**: The raw, chronological transcript of your life. In your app, this is the literal websocket chat transcript (`conversation_history`). It is messy and sequential: "On Tuesday at 4 PM, I told the AI my dog was sick."
*   **Semantic Memory**: Generalized facts abstracted away from the episode. You don't remember the exact conversation where you learned the sky is blue; you just know it. In your app, this is the `UserMemory` table (and the Knowledge Graph). The AI extracts facts from the episodic chat log (like "User has a dog" and "Dog is sick") and saves them as semantic knowledge.

### Reflection
Pioneered by the "Stanford Generative Agents" paper (Park et al., 2023), **Reflection** is the process where an agent periodically looks back at its raw memory stream and generates a higher-level summary. If an agent only remembers a giant list of raw facts (ate breakfast, talked to bob, saw a bird), it gets confused. Reflection distills those facts into actionable insights.

### Zep's Graphiti & Mem0
These are two cutting-edge tools currently popular in the industry:
*   **Graphiti** is a framework specifically designed to build Temporal Knowledge Graphs from conversations. It automatically handles invalidating old facts when the user changes their mind.
*   **Mem0** is a memory framework that argues different types of queries need different databases: Vector databases (for semantic similarity), Graph databases (for relationships), and Key-Value stores (for hard facts like usernames).

---

## 3. The "Continuity-Aware" Method (Our Approach)

In the text you pasted, we pivoted away from building a "Therapist AI" that psychoanalyzes the user, and towards building a **Continuity-Aware Assistant** that tracks the state of the user's life.

### Trait Inference (The Wrong Way)
Trying to infer deep psychological *traits* based on actions (e.g., "You missed your deadline, so you must be anxious"). This feels intrusive, judgmental, and is prone to LLM hallucination. Users hate this.

### State-Delta Detection (The Right Way)
"State" is what the Knowledge Graph looks like today. "Delta" is the mathematical term for *change*. 
Instead of analyzing the *user*, the AI analyzes the *Graph*. It diffs the Graph from today against the Graph from two weeks ago.
*   **Observation:** "Two weeks ago, the edge `[User] -(mentions)-> [Marathon]` was updated daily. In the last 14 days, it has not been updated at all."
This is a mechanical, factual observation that powers natural, non-intrusive follow-ups ("Hey, still on track for the marathon?").

### Mood as a "Valence Tag"
Rather than storing sentiment as a "fact" about the user ("The user is a sad person"), it is stored as a **Valence Tag** on a specific entity or edge.
*   The topic `JLPT N5` gets a tag `valence: trending_negative`. 
*   When the Assistant greets the user, it doesn't say "You are stressed." Instead, it uses the valence tag to alter its *tone*. It sees the negative valence on the exam topic and chooses to be delicate: "Hey, we haven't talked about the exam in a bit. No pressure, but do you want to review some flashcards later?"

---

## 4. Time Horizons and Differential Decay

A major flaw in basic memory systems is **Recency Bias**—they fixate on short-term noise and forget long-term goals just because they aren't mentioned daily.

### Tagging the "Horizon"
When extracting a goal into the graph, the system tags its temporal scale:
*   **Micro (Days):** Book driver's test today.
*   **Short-term (Weeks):** Dinner with Rachel next weekend.
*   **Medium/Long-term (Months/Years):** Pass the JLPT in December.

### Differential Decay
The system alters its "decay rate" (how fast it assumes a goal is abandoned due to silence) based on the Horizon.
*   **Micro Goal:** 48 hours of silence = likely over.
*   **Long-term Goal:** 48 hours of silence = completely normal. The system won't flag a "delta" unless there is a month of silence.

### Multi-Scale Reflection Sweeps
The background Reflection job runs with different lenses:
*   **Daily Sweep:** Checks on Micro/Short-term goals.
*   **Weekly Sweep:** Checks on Medium-term goals.
*   **Monthly Sweep:** Checks on Long-term goals and generates a "Pulse Check" reminder.

---

## 5. Autonomous Safeguards

When an AI has access to tool-calling, UX safeguards are critical.

### The "Ask First" Principle
An AI should not create background tasks autonomously just because it heard a related intent. If research fails, the SLM must report the failure and explicitly *ask* the user for permission before invoking a `create_task` tool. This prevents the silent accumulation of "ghost tasks."

### Structured Research Intents
When a polling or research task is created, a generic title is not enough. The task must contain a structured `research_intent` payload:
*   **Query:** The exact search string (e.g., "JLPT registration link December").
*   **Success Condition:** What constitutes a successful search (e.g., "Find a valid https URL for registration").
*   **On Failure:** The retry logic (e.g., "Bump deadline forward 7 days").
This allows the background scheduler to act deterministically without blindly burning API credits.

---

### Summary: What are we actually building?

We are building a **Temporal Semantic Graph with Delta-based Reflections.**

1.  **Extraction**: As the user talks, an LLM asynchronously extracts Nodes (Goals, People) and Edges (Relationships).
2.  **Resolution & Time**: It merges new mentions with existing Nodes. If a user contradicts an old fact ("I quit studying Japanese"), it doesn't delete the old fact; it caps its `valid_until` timestamp.
3.  **Nightly Reflection (Delta Detection)**: A background job compares the graph's recent changes. It looks for dropped topics, completed goals, or newly emerged habits. 
4.  **The Greeting**: When the user opens the app, the `/engagement/greeting` API pulls those recent *deltas* (changes) and uses them to ask a highly contextual, temporally aware question ("Still on track for...?"). 

Does this help clarify the terminology and the psychological/technical architecture we are aiming for? Let me know which part of this you'd like to dive deeper into!