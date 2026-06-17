/**
 * ToolActivity — live log of SLM→LLM escalation and tool calls.
 *
 * A test-harness panel (Milestone 1) so we can SEE the assistant escalate to the
 * tool-calling LLM and watch tools fire + their results while talking.
 */
export default function ToolActivity({ items }) {
  if (!items || items.length === 0) return null;

  return (
    <div style={styles.panel}>
      <div style={styles.heading}>🔧 Assistant activity</div>
      <div style={styles.list}>
        {items.map((item, i) => {
          if (item.kind === "escalate") {
            return (
              <div key={i} style={{ ...styles.chip, ...styles.escalate }}>
                🤔 thinking harder{item.text ? `: ${item.text}` : ""}
              </div>
            );
          }
          if (item.kind === "tool-start") {
            return (
              <div key={i} style={{ ...styles.chip, ...styles.toolStart }}>
                ▶ {item.name}…
              </div>
            );
          }
          // tool-result
          return (
            <div
              key={i}
              style={{ ...styles.chip, ...(item.ok ? styles.ok : styles.fail) }}
            >
              {item.ok ? "✓" : "✗"} {item.name}
              {item.summary ? ` — ${item.summary}` : ""}
            </div>
          );
        })}
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
    fontSize: "0.8rem",
  },
  heading: { fontWeight: 600, opacity: 0.7, marginBottom: "0.4rem" },
  list: { display: "flex", flexDirection: "column", gap: "0.3rem" },
  chip: { padding: "0.25rem 0.5rem", borderRadius: "6px", lineHeight: 1.3 },
  escalate: { background: "rgba(99,102,241,0.15)" },
  toolStart: { background: "rgba(234,179,8,0.18)" },
  ok: { background: "rgba(34,197,94,0.18)" },
  fail: { background: "rgba(239,68,68,0.18)" },
};
