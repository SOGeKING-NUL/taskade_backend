# System Design

## Overview
Taskade is a voice-first AI task manager that uses a single WebSocket connection for voice interactions. It employs a Llama 3.3 70B model hosted on Groq as the primary "brain" to process speech, manage tasks, and retrieve context from a Postgres database. 

The architecture consists of:
- **Client**: React-based frontend managing audio processing, voice activity detection, and UI.
- **Backend**: FastAPI server coordinating interactions.
- **Voice I/O**: Deepgram for STT (Speech-to-Text) and Sarvam Bulbul for TTS (Text-to-Speech).
- **Brain**: Groq Llama-3.3-70b as the core conversational logic, falling back to OpenRouter (gpt-4o-mini) when necessary.
- **Database**: PostgreSQL (via Supabase) storing user profiles, tasks, and memories.

## Architecture Diagram

![System Architecture](../client/public/sys_taskade.png)

## System Workflow
1. **User speaks**: Audio streams from the browser to the FastAPI backend over a WebSocket.
2. **STT**: Backend relays audio to Deepgram Nova-3 for live transcription and endpointing.
3. **Reasoning**: Once the utterance ends, the transcribed text and conversation history are sent to Groq Llama-3.3-70B.
4. **Action**: The LLM determines whether to respond directly or invoke tools (e.g., query database, web research).
5. **TTS**: As text is generated in chunks, it streams to Sarvam Bulbul for synthesis.
6. **Delivery**: Rendered audio returns via WebSocket to the React frontend for gapless playback.
