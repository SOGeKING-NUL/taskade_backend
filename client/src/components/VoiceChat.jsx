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
import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Orb } from "./Orb";
import LiveCaption from "./LiveCaption";
import TranscriptPage from "./TranscriptPage";
import ToolActivity from "./ToolActivity";
import MetadataCard from "./MetadataCard";
import { api } from "../utils/api";
import { VADManager } from "../utils/vadManager";
import { AudioPlayer } from "../utils/audioPlayer";

const STATUS_LABEL = {
  idle: "Starting…",
  listening: "Listening…",
  recording: "Hearing you…",
  processing: "Thinking…",
  speaking: "Speaking… (talk to interrupt)",
  muted: "Muted",
};

// Per-status orb palette (darker, lighter) — pastel lavender/purple family.
// State reads via warmth/intensity: dim idle → bright speaking; grey when muted.
const ORB_COLORS = {
  idle:       ["#4A4560", "#7D7499"],
  listening:  ["#8E76D0", "#B7A6E8"],
  recording:  ["#A98CE0", "#CDBEF2"],
  processing: ["#6F5DAE", "#9B8AD6"],
  speaking:   ["#B7A6E8", "#D8CCF5"],
  muted:      ["#33333B", "#55505F"],
};

const WS_URL =
  import.meta.env.VITE_WS_URL ||
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws/voice`;

// Pre-speech ring buffer: ~500ms of audio frames for capturing speech onset
const RING_BUFFER_FRAMES = 15; // 15 frames × 32ms = ~480ms

// Tools whose result can change the task list — used to trigger an
// automatic Tasks-page refresh instead of the page re-fetching on every visit.
const _TASK_MUTATING_TOOLS = new Set(["create_task", "update_task"]);

export default function VoiceChat({ accessToken, active = true, onEnd, onTaskChange }) {
  // ── State ──────────────────────────────────────────────────────────
  const [status, setStatus] = useState("idle"); // idle|listening|recording|processing|speaking|muted
  const [interimTranscript, setInterimTranscript] = useState("");
  const [responseCaption, setResponseCaption] = useState(""); // live AI answer, captioned as it's spoken
  const [isConnected, setIsConnected] = useState(false);
  const [messages, setMessages] = useState([]); // committed turns: { role, text, error? }
  const [toolActivity, setToolActivity] = useState([]); // this turn's tool chips
  const [metadataCards, setMetadataCards] = useState([]); // structured tool data
  const [notice, setNotice] = useState("");          // transient server notices
  const [dueReminders, setDueReminders] = useState([]); // due tasks from /reminders/due
  const [manualMute, setManualMute] = useState(false); // user-toggled mic mute
  const [transcriptOpen, setTranscriptOpen] = useState(false); // full-history overlay
  const noticeTimerRef = useRef(null);
  const mutedRef = useRef(false);                    // effective mute (manual OR not the active view)

  // ── Refs (survive re-renders, avoid stale closures) ────────────────
  const wsRef = useRef(null);
  const vadRef = useRef(null);
  const playerRef = useRef(new AudioPlayer());
  const audioElRef = useRef(null); // Hidden <audio> element for AEC
  const currentResponseRef = useRef("");
  const llmDoneTextRef = useRef("");
  const statusRef = useRef("idle");

  // Orb reactivity: live mic loudness, fed to the Orb's volume callbacks.
  const micLevelRef = useRef(0);

  // ── Streaming control refs ─────────────────────────────────────────

  const audioRingBufferRef = useRef([]);      // ring buffer for pre-speech capture

  // Keep statusRef in sync
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  // ── Orb volume callbacks (polled every frame by the Orb's render loop) ─
  // Input = your mic RMS; output = live TTS playback RMS. Voice RMS is small,
  // so boost into the orb's 0..1 range. The orb smooths internally.
  const getInputVolume = useCallback(() => {
    micLevelRef.current *= 0.95; // decay so a silent mic relaxes the orb
    return Math.min(1, micLevelRef.current * 4);
  }, []);

  const getOutputVolume = useCallback(() => {
    if (statusRef.current === "processing") {
      // No audio while thinking — gentle synthetic pulse instead.
      return 0.35 + 0.2 * Math.sin(performance.now() / 250);
    }
    return Math.min(1, playerRef.current.getLevel() * 2.5);
  }, []);

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
    setResponseCaption("");
  }, []);

  // Shared REST client (reminders live here; tasks are owned by the Tasks page).
  const rest = useMemo(() => api(accessToken), [accessToken]);

  // Transient notice line (auto-clears).
  const showNotice = useCallback((text, ms = 4000) => {
    clearTimeout(noticeTimerRef.current);
    setNotice(text);
    if (ms) noticeTimerRef.current = setTimeout(() => setNotice(""), ms);
  }, []);
  useEffect(() => () => clearTimeout(noticeTimerRef.current), []);

  // Due reminders — polled while the app is open. /reminders/due CONSUMES (marks
  // delivered), so each due item comes back exactly once; we show it as a card
  // AND fire a browser notification. NOTE: this only fires while the tab is open
  // — true background (tab-closed) push needs a service worker + web push, which
  // this web client intentionally doesn't do (that's the mobile client's job).
  //
  // Extracted to its own callback (not just inline in the effect) so a dev-only
  // "check now" button can trigger the EXACT same real path on demand, instead
  // of waiting up to 60s or faking a reminder client-side.
  const checkReminders = useCallback(async () => {
    try {
      const d = await rest.getReminders();
      if (!d.count) return;
      const due = d.tasks || [];
      setDueReminders((prev) => [...prev, ...due]);
      if ("Notification" in window && Notification.permission === "granted") {
        for (const t of due) {
          try {
            new Notification("Reminder", { body: t.title });
          } catch {
            /* notifications may be blocked */
          }
        }
      }
    } catch {
      /* ignore transient failures */
    }
  }, [rest]);

  useEffect(() => {
    if (!accessToken) return;
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-then-setState; resolves post-await, not synchronously
    checkReminders();
    const id = setInterval(checkReminders, 60000);
    return () => clearInterval(id);
  }, [accessToken, checkReminders]);

  // Effective mute = user toggle OR this isn't the active (visible) view. When
  // muted the mic is ignored and any in-flight reply is stopped — but the WS
  // stays connected, so returning to the Talk view resumes instantly.
  useEffect(() => {
    const muted = manualMute || !active;
    mutedRef.current = muted;
    if (muted) {
      playerRef.current.stop();
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "interrupt" }));
      }
      statusRef.current = "muted";
      // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: surface the mute toggle in the UI
      setStatus("muted");
    } else if (isConnected) {
      resumeListening();
    }
  }, [manualMute, active, isConnected, resumeListening]);

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
        if (data.text?.trim()) {
          setMessages((prev) => [...prev, { role: "user", text: data.text }]);
        }
        statusRef.current = "processing";
        setStatus("processing");
        // Fresh turn — clear the previous turn's tool activity + metadata.
        setToolActivity([]);
        setMetadataCards([]);
        break;

      // ── Tool-calling events ───────────────────────────────────
      case "tool.start":
        setToolActivity((prev) => [...prev, { kind: "start", name: data.name }]);
        break;

      case "tool.result":
        setToolActivity((prev) => [
          ...prev,
          { kind: "result", name: data.name, ok: data.ok, summary: data.summary },
        ]);
        if (data.ok && _TASK_MUTATING_TOOLS.has(data.name)) {
          onTaskChange?.();
        }
        break;

      case "metadata":
        // Structured data (links/dates) for a clickable card — never spoken.
        setMetadataCards((prev) => [...prev, { tool: data.tool, data: data.data }]);
        break;

      // ── LLM events ────────────────────────────────────────────
      case "llm.token":
        currentResponseRef.current += data.text;
        setResponseCaption(currentResponseRef.current);
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

        const finalText = llmDoneTextRef.current || currentResponseRef.current;
        if (finalText.trim()) {
          setMessages((prev) => [...prev, { role: "assistant", text: finalText }]);
        }
        currentResponseRef.current = "";
        llmDoneTextRef.current = "";
        // Clear the live caption immediately on commit — otherwise the finished
        // reply shows twice (committed + still-streaming caption) until audio ends.
        setResponseCaption("");

        // Wait for audio to finish playing, then resume listening.
        // If a barge-in happens meanwhile, status leaves "speaking" and we
        // must NOT overwrite the new state back to "listening".
        const checkDone = () => {
          // "speaking" = normal flow; "processing" = TTS produced no audio.
          // Anything else (e.g. "recording" after a barge-in) owns the state.
          if (statusRef.current !== "speaking" && statusRef.current !== "processing") return;
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
      case "notice":
        // Graceful server-side notice (e.g. model busy) — transient line.
        showNotice(data.message || "One moment…");
        break;

      case "stt.reconnecting":
        showNotice("Reconnecting…");
        break;

      case "history_cleared":
        setMessages([]);
        setToolActivity([]);
        setMetadataCards([]);
        break;

      case "error":
        console.error("Server error:", data.message);
        if (currentResponseRef.current.trim()) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", text: currentResponseRef.current, error: true },
          ]);
        }
        currentResponseRef.current = "";
        llmDoneTextRef.current = "";
        resumeListening();
        break;

      case "interrupted":
        playerRef.current.stop();
        currentResponseRef.current = "";
        llmDoneTextRef.current = "";
        setInterimTranscript("");
        setResponseCaption("");
        // Don't resume listening — the barge-in speech is still active
        break;

      default:
        break;
    }
  }, [resumeListening, showNotice, onTaskChange]);

  // ── WebSocket connection (with auto-reconnect) ─────────────────────
  useEffect(() => {
    let ws;
    let reconnectTimer;
    let closed = false;

    const connect = () => {
      if (closed || !accessToken) return;
      ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(accessToken)}`);
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
  }, [handleMessage, accessToken]);

  // ── VAD lifecycle ──────────────────────────────────────────────────
  useEffect(() => {
    if (!isConnected) return;

    const vad = new VADManager({
      onSpeechStart: () => {
        if (mutedRef.current) return; // muted: mic is ignored entirely
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
        if (mutedRef.current) return; // muted: don't send audio or feed the orb

        // Feed the orb: RMS of this mic frame (only while we're the speaker).
        const cs = statusRef.current;
        if (cs === "listening" || cs === "recording") {
          let sum = 0;
          for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
          micLevelRef.current = Math.sqrt(sum / frame.length);
        }

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

  // ── End session ──────────────────────────────────────────────────────
  // Unmounting runs both effect cleanups below: the WS effect sets its local
  // `closed` flag (so it won't auto-reconnect) and closes the socket, and the
  // VAD effect destroys the VAD instance and releases the microphone.
  const handleEndSession = () => {
    playerRef.current.stop();
    onEnd?.();
  };

  // Clear the conversation (server history + local transcript) without ending.
  const handleClearHistory = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "clear_history" }));
    }
    setMessages([]);
    setToolActivity([]);
    setMetadataCards([]);
  }, []);

  // ── Render ─────────────────────────────────────────────────────────
  // Stays MOUNTED and VISIBLE even when not the active view — `.inactive` just
  // dims it and disables clicks, so the orb keeps glowing faintly under the
  // translucent Tasks overlay (rather than disappearing via display:none).
  return (
    <div className={`voice-chat stage ${status} ${active ? "" : "inactive"}`}>
      <div className={`conn-dot ${isConnected ? "connected" : ""}`} title={isConnected ? "Connected" : "Disconnected"} />
      <button className="clear-btn" onClick={handleEndSession} title="End conversation">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 6h18" />
          <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
        </svg>
      </button>
      <button
        className="transcript-btn"
        onClick={() => setTranscriptOpen(true)}
        title="View full transcript"
        aria-label="View full transcript"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      </button>
      {/* Dev-only: manually run the exact same /reminders/due check the 60s
          poller uses, so a due reminder can be tested on demand instead of
          waiting for the interval or faking a reminder client-side. Stripped
          from production builds via import.meta.env.DEV. */}
      {import.meta.env.DEV && (
        <button
          className="dev-btn"
          onClick={checkReminders}
          title="Check reminders now (dev)"
          aria-label="Check reminders now (dev)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
            <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
          </svg>
        </button>
      )}
      {/* Fixed top-right, mirroring clear-btn — never clipped by the centered
          stage layout, unlike a flex child that can overflow on short screens. */}
      <button
        className={`mute-btn ${manualMute ? "on" : ""}`}
        onClick={() => setManualMute((m) => !m)}
        title={manualMute ? "Unmute microphone" : "Mute microphone"}
        aria-label={manualMute ? "Unmute microphone" : "Mute microphone"}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {manualMute ? (
            <>
              <path d="M2 2l20 20" />
              <path d="M9 5a3 3 0 0 1 6 0v6" />
              <path d="M19 10a7 7 0 0 1-1.3 4" />
              <path d="M12 19v3" />
            </>
          ) : (
            <>
              <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
              <path d="M19 10a7 7 0 0 1-14 0" />
              <path d="M12 19v3" />
            </>
          )}
        </svg>
      </button>

      {/* ── The reactive orb (ElevenLabs UI, volume-driven) ──────────── */}
      <div className="orb-wrap">
        <Orb
          colors={ORB_COLORS[status] || ORB_COLORS.idle}
          volumeMode="manual"
          getInputVolume={getInputVolume}
          getOutputVolume={getOutputVolume}
        />
      </div>

      <div className="stage-label">{STATUS_LABEL[status] || STATUS_LABEL.idle}</div>
      {notice && <div className="notice">{notice}</div>}
      <ToolActivity items={toolActivity} />

      {/* Bottom stack: due reminders, metadata cards, then the live transcript. */}
      <div className="stage-bottom">
        {dueReminders.length > 0 && (
          <div className="reminder-card">
            <div className="reminder-head">
              <span className="reminder-title">Due now</span>
              <button
                className="reminder-dismiss"
                onClick={() => setDueReminders([])}
                aria-label="Dismiss reminders"
              >
                ✕
              </button>
            </div>
            {dueReminders.map((t) => (
              <div key={t.id} className="reminder-row">{t.title}</div>
            ))}
          </div>
        )}
        <MetadataCard items={metadataCards} />
        <LiveCaption
          live={
            interimTranscript
              ? { role: "user", text: interimTranscript }
              : responseCaption
                ? { role: "assistant", text: responseCaption }
                : null
          }
        />
      </div>

      {transcriptOpen && (
        <TranscriptPage
          messages={messages}
          onClose={() => setTranscriptOpen(false)}
          onClear={handleClearHistory}
        />
      )}

      {/* Hidden <audio> element for AEC (Acoustic Echo Cancellation). */}
      <audio ref={audioElRef} autoPlay style={{ display: "none" }} />
    </div>
  );
}
