/**
 * LiveCaption — the ambient, ONE-turn-at-a-time caption: either your live
 * interim transcript while you talk, or the AI's answer captioned as it's
 * spoken. Never both, never history — that's what TranscriptPage is for.
 */
export default function LiveCaption({ live }) {
  if (!live) return null;
  return (
    <div className="caption">
      <p className={`t-msg ${live.role}`}>{live.text}</p>
    </div>
  );
}
