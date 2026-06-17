/**
 * vadManager.js — Thin wrapper around @ricky0123/vad-web (Silero VAD).
 *
 * In the hybrid architecture, VAD has ONE job: instant barge-in detection.
 * End-of-turn detection is handled server-side by Deepgram.
 *
 * The VAD also exposes every audio frame via onFrameProcessed so the
 * client can stream raw PCM to the server continuously.
 */
import { MicVAD } from "@ricky0123/vad-web";
import * as ort from "onnxruntime-web";

// Configure ONNX Runtime to load WASM files from the public root directory
ort.env.wasm.wasmPaths = "/";

/**
 * @typedef {Object} VADCallbacks
 * @property {() => void}                        onSpeechStart     - Fires instantly when voice is detected.
 * @property {(audio: Float32Array) => void}     onSpeechEnd       - Fires when silence follows speech.
 * @property {(probs: Object, frame: Float32Array) => void} onFrameProcessed - Every ~32ms audio frame.
 */

export class VADManager {
  /** @param {VADCallbacks} callbacks */
  constructor(callbacks) {
    this.callbacks = callbacks;
    /** @type {MicVAD | null} */
    this.vad = null;
  }

  /**
   * Request mic permission and start the VAD pipeline.
   * Resolves once the mic stream is active.
   */
  async start() {
    this.vad = await MicVAD.new({
      model: "v5",
      modelURL: "/silero_vad_v5.onnx",
      workletURL: "/vad.worklet.bundle.min.js",

      // ── Detection thresholds ───────────────────────────────────
      // These are tuned for BARGE-IN only. We don't care about
      // redemptionMs for end-of-turn — Deepgram handles that.
      positiveSpeechThreshold: 0.8,   // high confidence needed to trigger
      negativeSpeechThreshold: 0.3,   // drop threshold
      minSpeechMs: 150,               // ignore clicks/pops shorter than 150ms
      preSpeechPadMs: 0,              // we handle pre-speech via ring buffer
      redemptionMs: 600,              // only affects onSpeechEnd timing (UI hint)

      // ── Audio constraints ──────────────────────────────────────
      getStream: async () => {
        return await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            channelCount: 1,
            sampleRate: 16000,
          },
        });
      },



      // ── Callbacks ──────────────────────────────────────────────
      onSpeechStart: () => {
        this.callbacks.onSpeechStart?.();
      },

      onSpeechEnd: (audio) => {
        this.callbacks.onSpeechEnd?.(audio);
      },

      onFrameProcessed: (probs, frame) => {
        this.callbacks.onFrameProcessed?.(probs, frame);
      },
    });

    // The VAD will start automatically because startOnLoad is true by default
  }

  /** Fully tear down the VAD pipeline and release the microphone. */
  destroy() {
    if (this.vad) {
      this.vad.destroy();
      this.vad = null;
    }
  }
}
