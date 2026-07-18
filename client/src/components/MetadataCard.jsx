/**
 * MetadataCard — structured data surfaced from a tool result (links, dates,
 * findings) as a clickable card, separate from the spoken answer. This is the
 * voice-first "show the link, don't recite it" channel.
 */
import { stripMarkdown } from "../utils/markdown";

function href(link) {
  return typeof link === "string" ? link : link?.url;
}
function label(link) {
  if (typeof link === "string") return link;
  return link?.label || link?.url;
}

export default function MetadataCard({ items }) {
  if (!items.length) return null;
  const tasks = items.flatMap((m) => m.data?.tasks || []);
  const research = items.filter((m) => m.data?.research).map((m) => m.data.research);
  if (!tasks.length && !research.length) return null;

  return (
    <div className="meta-cards">
      {research.map((r, i) => (
        <div key={`r-${i}`} className="meta-card">
          {r.summary && <div className="meta-row">{stripMarkdown(r.summary)}</div>}
          {(r.links || []).map((l, j) => {
            const url = href(l);
            if (!url) return null;
            return (
              <a key={j} className="meta-link" href={url} target="_blank" rel="noreferrer">
                {label(l)}
              </a>
            );
          })}
        </div>
      ))}
      {tasks.map((t, i) => (
        <div key={`t-${i}`} className="meta-card">
          <div className="meta-title">{t.title}</div>
          {t.due_at && (
            <div className="meta-row">Due {new Date(t.due_at).toLocaleString()}</div>
          )}
          {t.summary && <div className="meta-row dim">{t.summary}</div>}
          {(t.links || []).map((l, j) => {
            const url = href(l);
            if (!url) return null;
            return (
              <a
                key={j}
                className="meta-link"
                href={url}
                target="_blank"
                rel="noreferrer"
              >
                {label(l)}
              </a>
            );
          })}
        </div>
      ))}
    </div>
  );
}
