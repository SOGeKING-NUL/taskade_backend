/**
 * MetadataCard — renders structured task details (links, findings, due date)
 * as a separate, clickable card.
 *
 * Deliberately narrow: the backend only emits this when the user explicitly
 * asked about ONE specific, already-tracked task (`query_tasks` with
 * `scope=specific_task`) — never as a side effect of research running or a
 * task being created/updated. This is the durable substitute for relying on
 * the model to recite links/amounts aloud — the data arrives on a
 * deterministic WS channel (`metadata`) and is rendered here, separate from
 * the chat bubble.
 */
export default function MetadataCard({ items }) {
  if (!items || items.length === 0) return null;

  return (
    <div style={styles.container}>
      {items.map((meta, i) => (
        <div key={i} style={styles.card}>
          {/* ── Task details ─────────────────────────────────────── */}
          {meta.data?.tasks?.map((t, k) => (
            <div key={k} style={styles.section}>
              {t.title && (
                <div style={styles.taskTitle}>📋 {t.title}</div>
              )}
              {t.due_at && (
                <div style={styles.detail}>
                  📅 Due: {new Date(t.due_at).toLocaleString()}
                </div>
              )}
              {t.summary && (
                <div style={styles.detail}>📝 {t.summary}</div>
              )}
              {t.links?.map((link, m) => {
                const url = typeof link === "string" ? link : link?.url;
                const label =
                  typeof link === "string"
                    ? link
                    : link?.label || link?.url || "Link";
                return url ? (
                  <a
                    key={m}
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={styles.link}
                  >
                    🔗 {label}
                  </a>
                ) : null;
              })}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

const styles = {
  container: {
    margin: "0.5rem 1rem",
    display: "flex",
    flexDirection: "column",
    gap: "0.4rem",
  },
  card: {
    padding: "0.6rem 0.75rem",
    background: "rgba(59,130,246,0.08)",
    borderRadius: "10px",
    border: "1px solid rgba(59,130,246,0.15)",
    fontSize: "0.82rem",
  },
  section: {
    marginBottom: "0.4rem",
  },
  label: {
    fontWeight: 600,
    fontSize: "0.72rem",
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    opacity: 0.6,
    marginBottom: "0.2rem",
  },
  findings: {
    lineHeight: 1.4,
    opacity: 0.9,
  },
  link: {
    display: "block",
    color: "#3b82f6",
    textDecoration: "none",
    padding: "0.15rem 0",
    wordBreak: "break-all",
    fontSize: "0.78rem",
  },
  taskTitle: {
    fontWeight: 600,
    marginBottom: "0.2rem",
  },
  detail: {
    opacity: 0.85,
    padding: "0.1rem 0",
    fontSize: "0.78rem",
  },
};
