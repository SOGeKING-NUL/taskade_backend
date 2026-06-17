/**
 * TasksPanel — live view of persisted tasks (Milestone 2).
 *
 * Reads from the backend GET /tasks endpoint so we can watch tasks survive
 * restarts and update as the assistant creates/completes them by voice.
 */
const STATUS_COLORS = {
  pending: "rgba(234,179,8,0.18)",
  blocked: "rgba(148,163,184,0.22)",
  active: "rgba(59,130,246,0.18)",
  done: "rgba(34,197,94,0.18)",
  cancelled: "rgba(239,68,68,0.15)",
};

export default function TasksPanel({ tasks }) {
  if (!tasks || tasks.length === 0) return null;

  return (
    <div style={styles.panel}>
      <div style={styles.heading}>🗂️ Tasks ({tasks.length})</div>
      <div style={styles.list}>
        {tasks.map((t) => (
          <div
            key={t.id}
            style={{
              ...styles.row,
              marginLeft: t.parent_title ? "1.1rem" : 0,
            }}
          >
            <span style={{ ...styles.badge, background: STATUS_COLORS[t.status] || "#eee" }}>
              {t.status}
            </span>
            <span style={styles.title}>
              {t.parent_title ? "↳ " : ""}
              {t.title}
            </span>
            {t.due_at && (
              <span style={styles.due}>{new Date(t.due_at).toLocaleDateString()}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

const styles = {
  panel: {
    margin: "0.5rem 1rem",
    padding: "0.5rem 0.75rem",
    background: "rgba(0,0,0,0.04)",
    borderRadius: "10px",
    fontSize: "0.82rem",
  },
  heading: { fontWeight: 600, opacity: 0.7, marginBottom: "0.4rem" },
  list: { display: "flex", flexDirection: "column", gap: "0.3rem" },
  row: { display: "flex", alignItems: "center", gap: "0.5rem" },
  badge: {
    padding: "0.1rem 0.45rem",
    borderRadius: "5px",
    fontSize: "0.7rem",
    textTransform: "uppercase",
    letterSpacing: "0.02em",
    minWidth: "62px",
    textAlign: "center",
  },
  title: { flex: 1 },
  due: { opacity: 0.6, fontSize: "0.72rem" },
};
