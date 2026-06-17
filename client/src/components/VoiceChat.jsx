/**
 * VoiceChat — Always-on voice conversation component.
 *
 * Hybrid VAD Architecture:
 *   Client Silero VAD (barge-in) → Continuous audio stream → Server
 *   Server: Deepgram STT (endpointing) → LLM → Sarvam TTS → Client audio
 *
 * No buttons needed — speech is detected automatically via client-side
 * Voice Activity Detection. Supports instant barge-in interruption.
 */
import { useState, useEffect, useRef, useCallback } from "react";
import ChatMessage from "./ChatMessage";
import StatusIndicator from "./StatusIndicator";
import ToolActivity from "./ToolActivity";
import { VADManager } from "../utils/vadManager";
import { AudioPlayer } from "../utils/audioPlayer";

const WS_URL =
  import.meta.env.VITE_WS_URL ||
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws/voice`;

// Pre-speech ring buffer: ~500ms of audio frames for capturing speech onset
const RING_BUFFER_FRAMES = 15; // 15 frames × 32ms = ~480ms

export default function VoiceChat() {
  // ── State ──────────────────────────────────────────────────────────
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState("idle"); // idle|listening|recording|processing|speaking
  const [currentResponse, setCurrentResponse] = useState("");
  const [interimTranscript, setInterimTranscript] = useState("");
  const [isConnected, setIsConnected] = useState(false);
  const [toolActivity, setToolActivity] = useState([]); // escalation + tool-call log

  // ── Refs (survive re-renders, avoid stale closures) ────────────────
  const wsRef = useRef(null);
  const vadRef = useRef(null);
  const playerRef = useRef(new AudioPlayer());
  const audioElRef = useRef(null); // Hidden <audio> element for AEC
  const currentResponseRef = useRef("");
  const llmDoneTextRef = useRef("");
  const messagesEndRef = useRef(null);
  const statusRef = useRef("idle");

  // ── Streaming control refs ─────────────────────────────────────────

  const audioRingBufferRef = useRef([]);      // ring buffer for pre-speech capture

  // Keep statusRef in sync
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  // ── Auto-scroll ────────────────────────────────────────────────────
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, currentResponse, interimTranscript]);

  // ── Helper: convert Float32 frame → PCM16 and send via WebSocket ──
  const frameCountRef = useRef(0);
  
  const sendAudioFrame = useCallback((frame) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const pcm16 = new Int16Array(frame.length);
    for (let i = 0; i < frame.length; i++) {
      const s = Math.max(-1, Math.min(1, frame[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    ws.send(pcm16.buffer);
    
    frameCountRef.current++;
    if (frameCountRef.current === 1) {
      console.log("Started sending audio frames to server!");
    } else if (frameCountRef.current % 100 === 0) {
      console.log(`Sent ${frameCountRef.current} audio frames so far...`);
    }
  }, []);

  // ── Resume listening (called after TTS finishes) ──────────────────
  const resumeListening = useCallback(() => {
    statusRef.current = "listening";
    setStatus("listening");

    setInterimTranscript("");
  }, []);

  // ── WebSocket message handler ──────────────────────────────────────
  const handleMessage = useCallback((data) => {
    switch (data.type) {
      case "processing":
        setStatus("processing");
        statusRef.current = "processing";
        break;

      case "stt.interim":
        // Real-time partial transcript — show as ghost text
        setInterimTranscript(data.text);
        if (statusRef.current === "listening") {
          setStatus("recording");
          statusRef.current = "recording";
        }
        break;

      case "stt.final":
        // Finalized segment (but utterance may continue)
        setInterimTranscript(data.text);
        break;

      case "stt.result":
        // Deepgram utterance_end — final transcript, stop streaming
        setInterimTranscript("");
        setMessages((prev) => [...prev, { role: "user", text: data.text }]);
    
        statusRef.current = "processing";
        setStatus("processing");
        // Fresh turn — clear the previous turn's activity log
        setToolActivity([]);
        break;

      // ── Tool-calling events (Milestone 1 test harness) ────────
      case "escalated":
        setToolActivity((prev) => [...prev, { kind: "escalate", text: data.intent }]);
        break;

      case "tool.start":
        setToolActivity((prev) => [...prev, { kind: "tool-start", name: data.name }]);
        break;

      case "tool.result":
        setToolActivity((prev) => [
          ...prev,
          { kind: "tool-result", name: data.name, ok: data.ok, summary: data.summary },
        ]);
        break;

      // ── LLM events ────────────────────────────────────────────
      case "llm.token":
        setCurrentResponse((prev) => {
          const next = prev + data.text;
          currentResponseRef.current = next;
          return next;
        });
        break;

      case "llm.done":
        llmDoneTextRef.current = data.text || "";
        break;

      // ── TTS events ────────────────────────────────────────────
      case "tts.start":
        setStatus("speaking");
        statusRef.current = "speaking";
        // Reset player for fresh playback session
        playerRef.current.stop();
        if (data.sampleRate) {
          playerRef.current.setSampleRate(data.sampleRate);
        }
        break;

      case "tts.done": {
        // Flush any remaining pre-buffered audio chunks
        playerRef.current.flushAndPlay();

        const text = llmDoneTextRef.current || currentResponseRef.current;
        if (text) {
          setMessages((prev) => [...prev, { role: "assistant", text }]);
        }
        setCurrentResponse("");
        currentResponseRef.current = "";
        llmDoneTextRef.current = "";

        // Wait for audio to finish playing, then resume listening
        const checkDone = () => {
          if (playerRef.current.isActive()) {
            setTimeout(checkDone, 200);
          } else {
            resumeListening();
          }
        };
        checkDone();
        break;
      }

      // ── Control events ────────────────────────────────────────
      case "error":
        console.error("Server error:", data.message);
        if (currentResponseRef.current) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", text: currentResponseRef.current, error: true },
          ]);
        }
        setCurrentResponse("");
        currentResponseRef.current = "";
        llmDoneTextRef.current = "";
        resumeListening();
        break;

      case "interrupted":
        playerRef.current.stop();
        setCurrentResponse("");
        currentResponseRef.current = "";
        llmDoneTextRef.current = "";
        setInterimTranscript("");
        // Don't resume listening — the barge-in speech is still active
        break;

      case "history_cleared":
        setMessages([]);
        setToolActivity([]);
        break;

      default:
        break;
    }
  }, [resumeListening]);

  // ── WebSocket connection (with auto-reconnect) ─────────────────────
  useEffect(() => {
    let ws;
    let reconnectTimer;
    let closed = false;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(WS_URL);
      ws.binaryType = "arraybuffer";

      ws.onopen = () => {
        if (closed) return;
        console.log("WS connected");
        setIsConnected(true);
        setStatus("listening");
        statusRef.current = "listening";
      };

      ws.onmessage = (event) => {
        if (closed) return;
        if (typeof event.data === "string") {
          try {
            handleMessage(JSON.parse(event.data));
          } catch (err) {
            console.error("Failed to parse WS message:", err);
          }
        } else if (event.data instanceof ArrayBuffer) {
          // Binary audio from TTS — queue for playback
          playerRef.current.playChunk(event.data);
        }
      };

      ws.onclose = () => {
        console.log("WS disconnected");
        if (closed) return; // Prevent state corruption from unmounted StrictMode components
        
        setIsConnected(false);
        setStatus("idle");
        statusRef.current = "idle";
        if (wsRef.current === ws) {
          wsRef.current = null;
        }
        reconnectTimer = setTimeout(connect, 2000);
      };

      ws.onerror = (err) => {
        if (closed) return;
        console.error("WS error:", err);
        ws.close();
      };

      wsRef.current = ws;
    };

    connect();

    return () => {
      closed = true;
      clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, [handleMessage]);

  // ── VAD lifecycle ──────────────────────────────────────────────────
  useEffect(() => {
    if (!isConnected) return;

    const vad = new VADManager({
      onSpeechStart: () => {
        const currentStatus = statusRef.current;
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;

        // Guard: ignore if already recording or processing
        if (currentStatus === "recording" || currentStatus === "processing") return;

        // Barge-in: if AI is speaking, interrupt it first
        if (currentStatus === "speaking") {
          playerRef.current.stop();
          ws.send(JSON.stringify({ type: "interrupt" }));
          
          // Since we weren't streaming while speaking, flush the ring buffer
          // to Deepgram so it catches the words that triggered the barge-in
          for (const bufferedFrame of audioRingBufferRef.current) {
            sendAudioFrame(bufferedFrame);
          }
        }
        
        audioRingBufferRef.current = [];

        // Update ref synchronously BEFORE async React setState
        statusRef.current = "recording";
        
        // Let server know VAD thinks speech started (optional, since Deepgram handles it, but good for logs/UI)
        ws.send(JSON.stringify({ type: "speech.start" }));
        setStatus("recording");
        setInterimTranscript("");
      },

      onSpeechEnd: () => {
        // In the hybrid architecture, we do NOT stop streaming here.
        // Deepgram handles end-of-turn detection on the server.
        // Just update the UI indicator as a visual hint.
        if (statusRef.current === "recording") {
          // Keep streaming! Don't set isStreamingRef to false.
          // Deepgram needs to "hear" the silence to fire utterance_end.
        }
      },

      onFrameProcessed: (probs, frame) => {
        if (!frame) return;

        // Keep a rolling buffer of ~500ms
        audioRingBufferRef.current.push(new Float32Array(frame));
        if (audioRingBufferRef.current.length > RING_BUFFER_FRAMES) {
          audioRingBufferRef.current.shift();
        }

        const currentStatus = statusRef.current;
        if (audioRingBufferRef.current.length === 1) {
           console.log("VAD is processing frames! Current status:", currentStatus);
        }
        
        // In the hybrid architecture, we stream microphone audio to Deepgram
        // as long as we are listening/recording. Deepgram handles VAD server-side.
        if (currentStatus === "listening" || currentStatus === "recording") {
          sendAudioFrame(frame);
        }
      },
    });

    vadRef.current = vad;

    // Start VAD (requests mic permission)
    vad.start().then(() => {
      console.log("VAD started — listening for speech");
      // Resume AudioContext (needs user gesture)
      playerRef.current.resume();

      // Bind AudioPlayer output to hidden <audio> element for AEC
      const stream = playerRef.current.getOutputStream();
      if (audioElRef.current && stream) {
        audioElRef.current.srcObject = stream;
      }
    }).catch((err) => {
      console.error("VAD start failed:", err);
      alert("Microphone permission is required for voice chat.");
    });

    return () => {
      vad.destroy();
      vadRef.current = null;
      audioRingBufferRef.current = [];
    };
  }, [isConnected, sendAudioFrame]);

  // ── Clear history handler ──────────────────────────────────────────
  const handleClearHistory = () => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "clear_history" }));
    }
    setMessages([]);
    setToolActivity([]);
  };

  // ── Render ─────────────────────────────────────────────────────────
  return (
    <div className="voice-chat">
      {/* ── Header ─────────────────────────────────────────────────── */}
      <header className="header">
        <div className="header-content">
          <h1 className="title">Voice AI</h1>
          <p className="subtitle">Speak naturally, get intelligent answers</p>
          <div className={`status-dot ${isConnected ? "connected" : ""}`} />
          {messages.length > 0 && (
            <button
              className="clear-btn"
              onClick={handleClearHistory}
              title="Clear conversation"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M3 6h18" />
                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
              </svg>
            </button>
          )}
        </div>
      </header>

      {/* ── Messages ───────────────────────────────────────────────── */}
      <main className="messages" id="messages-container">
        {messages.length === 0 && !currentResponse && !interimTranscript && (
          <div className="empty-state">
            <div className="empty-icon">🎙️</div>
            <p className="empty-text">Just start speaking — I'm listening</p>
          </div>
        )}

        {messages.map((msg, i) => (
          <ChatMessage key={i} message={msg} />
        ))}

        {/* Interim transcript — shows what user is saying in real-time */}
        {interimTranscript && (
          <ChatMessage
            message={{ role: "user", text: interimTranscript }}
            streaming
          />
        )}

        {currentResponse && (
          <ChatMessage
            message={{ role: "assistant", text: currentResponse }}
            streaming
          />
        )}

        <div ref={messagesEndRef} />
      </main>

      {/* ── Tool activity (Milestone 1 test harness) ─────────────────── */}
      <ToolActivity items={toolActivity} />

      {/* ── Status Indicator ─────────────────────────────────────────── */}
      <footer className="controls">
        <StatusIndicator status={status} />
      </footer>

      {/* Hidden <audio> element for AEC (Acoustic Echo Cancellation). */}
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <audio ref={audioElRef} autoPlay style={{ display: "none" }} />
    </div>
  );
}
