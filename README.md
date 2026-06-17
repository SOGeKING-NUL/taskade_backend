# Voice AI — Low-Latency Real-Time TTS & STT Speech Engine

This project is a high-performance, real-time voice conversational agent. It enables full-duplex voice interactions with sub-second latency by chaining **Speech-to-Text (STT)**, a **Large Language Model (LLM)**, and **Text-to-Speech (TTS)** in a concurrent streaming pipeline.

---

## Core Architecture & Execution Flow

```
   [ User Microphone ] 
           │ (Float32 Audio Capture)
           ▼
   [ AudioRecorder ]
           │ (Downsamples & converts to Mono Int16 PCM)
           ▼  WebSocket (Binary / JSON Control)
    ┌───────────────────────────┐
    │   FastAPI Backend         │
    │                           │
    │   1. [ Sarvam STT ] ◄────┼── REST Request (Transcribes full audio)
    │      Returns text transcript
    │                           │
    │   2. [ Gemini Flash LLM ] ├── Streaming Token Generator
    │      Yields tokens incrementally
    │                           │
    │   3. [ Sentence Chunker ] ├── Splits streams at sentence boundaries (. ! ? ; :)
    │      Yields clean text sentences
    │                           │
    │   4. [ Sarvam TTS WS ] ──┼── WebSocket Stream (Sends sentences / flushes)
    │      Receives base64 WAV chunks
    │                           │
    │   5. [ Header Stripper ] ┼── Decodes base64, strips 44-byte WAV header
    │      Yields raw Mono Int16 PCM
    └──────────┬────────────────┘
               │  WebSocket (Binary Audio Chunks)
               ▼
      [ AudioPlayer ]
               │ (Converts Int16 to Float32, queues in Jitter Buffer)
               ▼
     [ Web Audio API ] (Sample-accurate AudioBufferSourceNode scheduling)
```

---

## Key Latency Optimization Mechanisms

### 1. Direct PCM Pipeline (Zero-Decode Overhead)
Compressing audio to MP3 or Opus introduces framing delays and CPU decompression overhead. This system uses raw **Mono Int16 PCM (LPCM)** at the boundary.
- **Microphone Input**: Downsampled to a clean sample rate and sent to the server.
- **TTS Output**: The backend requests the `wav` codec from Sarvam's TTS API, decodes the base64 output, and strips the 44-byte WAV header. The raw PCM16 samples are sent to the client, which plays them immediately.

### 2. Sentence-Boundary Chunking
Instead of waiting for the LLM to generate a full response, the backend splits the streaming token stream into sentences using a set of punctuation boundaries (`.!?।;:`).
- Once a sentence reaches a threshold length (>5 characters) and ends with punctuation, it is instantly pushed to the TTS processing queue.
- This overlapping pipeline executes **LLM generation and TTS synthesis concurrently**.

### 3. Client-Side Jitter Pre-Buffering
To handle network latency fluctuations without audible cuts:
- The client-side `AudioPlayer` queues incoming audio chunks.
- Playback is held until **2 chunks** are fully loaded. This absorbs initial network jitter and ensures a continuous audio stream.

### 4. Sample-Accurate Look-Ahead Scheduling
Browsers cannot play separate audio nodes back-to-back without gap pops if scheduled on simple event handlers.
- The player schedules each `AudioBufferSourceNode` precisely on the `AudioContext` timeline using `nextStartTime`.
- A **50ms look-ahead buffer** prevents the audio clock from falling behind the system clock, achieving seamless, gapless playback.

### 5. Instant Barge-in (Interrupt Handling)
True conversation requires the ability to interrupt the AI.
- When the user starts speaking or clicks the mic, the client sends an `interrupt` control frame.
- The server instantly cancels the active pipeline task (STT/LLM/TTS operations) and sends a confirmation frame.
- The client-side player immediately stops all active audio nodes, resetting the system to a clean listening state.
