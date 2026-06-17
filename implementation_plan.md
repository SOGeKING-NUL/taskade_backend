# Backend Architecture & Feature Plan: Voice-First Assistant

This document outlines the detailed backend architecture, addressing how the multi-stage task lifecycle operates entirely on the server, the strategy for the memory layer (Graph vs. Vector), and a breakdown of the specific features we will build.

## User Review Required

Please review the **Memory Architecture** section. We have a choice between keeping the stack simple (Postgres + pgvector) or introducing a dedicated Graph Database (like Neo4j) to map complex relationships.

## 1. Backend-Only Task Loop

You are completely right: the task lifecycle must be driven entirely by the backend. The frontend is just a "dumb" terminal for voice calls. Here is how the three steps execute on the server:

### Step 1: Ingestion & Research (The Agent Router)
1.  **Audio In**: User speaks to the app. The audio streams via WebRTC to our backend.
2.  **STT & Intent**: Backend converts speech to text. An SLM (Small Language Model) analyzes the intent.
3.  **Research Tool**: If the task requires external info (like the JLPT exam dates), the SLM triggers an LLM-powered "Research Tool" to scrape the web and return structured JSON (dates, deadlines, URLs).

### Step 2: Task State Machine (Database)
Instead of just "setting a reminder date", the backend breaks the researched data into a **Task Tree** in the PostgreSQL database.
*   **Parent Task**: JLPT Exam (Deadline: Dec 7).
*   **Child Task 1**: Registration Window (Status: `pending_registration`, Window: Sep 1 - Sep 15).
*   **Child Task 2**: Study Plan (Status: `blocked_by_registration`).

### Step 3: The Orchestrator (Daily Loop)
1.  **Cron/Background Worker**: A backend scheduler (e.g., `APScheduler` or a Celery worker) runs every hour.
2.  **State Evaluation**: It queries the PostgreSQL `tasks` table: *Which tasks have entered their action window based on today's date?*
3.  **Proactive Outreach**: If Child Task 1 (Registration) is active today, the backend triggers an outgoing WebRTC call to the user via LiveKit, passing the context to the TTS engine: *"Hey, the JLPT registration opened today. Here is the link..."*

---

## 2. Memory Layer: Graph DB vs. Vector DB

You raised an excellent point about using a Knowledge Graph to understand semantic relationships better. 

**Vector DBs (like Pinecone, pgvector)** are great for *semantic similarity*. If you say "I need to study," it can find a past memory like "User is taking a Japanese test."
**Graph DBs (like Neo4j)** are great for *explicit relationships*. (User) -[REGISTERED_FOR]-> (JLPT Exam) -[REQUIRES]-> (Passport).

**Recommendation: The Hybrid Approach**
Do we *need* Pinecone or a dedicated Graph DB? 
1.  **Option A (Simplest & Recommended for MVP):** We use **PostgreSQL with the `pgvector` extension** combined with a framework like **Mem0**. Mem0 is an open-source memory layer that natively handles extracting both vector embeddings AND graph-like relationships and can store them together in Postgres. This gives you the power of semantic search and relationship mapping without managing three different database clusters.
2.  **Option B (Enterprise Grade):** Use **Neo4j**. Neo4j recently added vector search capabilities. This means it acts as a Graph DB natively, but can also do Pinecone-style vector lookups in the same query. 

*For this build, I suggest Option A: PostgreSQL + pgvector + Mem0 to handle both relational tables and complex memory graphs in one unified place.*

---

## 3. Features We Will Build

Here is the exact feature list we will implement on the backend to make this work:

### Core Infrastructure
*   **FastAPI Web Server**: The central hub handling requests and API routes.
*   **LiveKit Integration**: A WebRTC server setup to handle real-time, low-latency audio streams between the backend and the mobile app.

### AI & Voice Pipeline
*   **STT/TTS Services**: Integration with Deepgram (or similar) for fast Speech-to-Text and a high-quality Text-to-Speech provider for the agent's voice.
*   **SLM/LLM Router**: The logic layer that decides if a user's prompt can be answered immediately, requires web research, or requires updating a database task.

### Task Management Engine
*   **PostgreSQL Database Models**: SQLAlchemy schemas for the `tasks` table, supporting parent/child relationships, states, and completion conditions.
*   **Background Scheduler**: `APScheduler` integration to run the continuous loop that checks for due tasks and initiates proactive agent calls.

### Memory System
*   **Mem0 Integration**: A memory extraction pipeline that listens to conversations and silently saves facts and entity relationships to the database via `pgvector`.
