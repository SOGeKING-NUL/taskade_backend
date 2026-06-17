/**
 * StatusIndicator — animated visual feedback for the voice session state.
 *
 * Replaces the old MicButton. No click handler needed — the system is
 * always-on. Shows animated pulses, waveforms, and spinners based on
 * the current status.
 */
export default function StatusIndicator({ status }) {
  const config = {
    idle:       { label: "Starting…",                  className: "idle" },
    listening:  { label: "Listening…",                 className: "listening" },
    recording:  { label: "Hearing you…",               className: "recording" },
    processing: { label: "Thinking…",                  className: "processing" },
    speaking:   { label: "Speaking… (talk to interrupt)", className: "speaking" },
  };

  const { label, className } = config[status] || config.idle;

  return (
    <div className={`status-indicator ${className}`} id="status-indicator">
      <div className="indicator-orb">
        <div className="orb-core" />
        <div className="orb-ring ring-1" />
        <div className="orb-ring ring-2" />
        <div className="orb-ring ring-3" />
      </div>
      <span className="indicator-label">{label}</span>
    </div>
  );
}
