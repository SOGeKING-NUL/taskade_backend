/**
 * TranscriptPage — full committed conversation history, behind a toggle
 * (the ambient view only ever shows the current turn via LiveCaption).
 * Translucent overlay over the orb, matching the Tasks page pattern.
 */
export default function TranscriptPage({ messages, onClose, onClear }) {
  return (
    <div className="page">
      <div className="page-head">
        <span className="page-title">Transcript</span>
        <div className="page-head-actions">
          {messages.length > 0 && (
            <button className="page-refresh" onClick={onClear} title="Clear">
              🗑
            </button>
          )}
          <button className="page-refresh" onClick={onClose} aria-label="Close" title="Close">
            ✕
          </button>
        </div>
      </div>
      <div className="page-body">
        {messages.length === 0 && <p className="page-empty">Nothing said yet.</p>}
        {messages.map((m, i) => (
          <p key={i} className={`t-msg ${m.role}${m.error ? " error" : ""}`}>
            {m.text}
          </p>
        ))}
      </div>
    </div>
  );
}
