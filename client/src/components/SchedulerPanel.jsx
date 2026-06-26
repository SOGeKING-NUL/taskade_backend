/**
 * SchedulerPanel — debug surface for scheduler jobs + research-retry schedule.
 *
 * Polls GET /debug/scheduler every 30s while mounted.  Shows:
 *   • APScheduler jobs (id, next run time, trigger type)
 *   • Per-task research retry schedule (query, next attempt, last outcome)
 *
 * Styled consistently with ToolActivity / TasksPanel.
 */
import { useState, useEffect, useCallback } from "react";

const POLL_INTERVAL_MS = 30_000;

export default function SchedulerPanel({ accessToken, apiBase }) {
  const [data, setData] = useState(null);
  const [expanded, setExpanded] = useState(false);

  const fetchScheduler = useCallback(async () => {
    try {
      const res = await fetch(`${apiBase}/debug/scheduler`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (res.ok) {
        setData(await res.json());
      }
    } catch (err) {
      console.warn("Failed to fetch scheduler status:", err);
    }
  }, [accessToken, apiBase]);

  // Initial fetch + polling interval.
  useEffect(() => {
    fetchScheduler();
    const id = setInterval(fetchScheduler, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [fetchScheduler]);

  if (!data) return null;

  const { jobs = [], research_schedule: research = [] } = data;

  return (
    <div style={styles.panel}>
      <div
        style={styles.heading}
        onClick={() => setExpanded((e) => !e)}
        role="button"
        tabIndex={0}
      >
        ⏱️ Scheduler {expanded ? "▾" : "▸"}
      </div>

      {expanded && (
        <>
          {/* ── Jobs ────────────────────────────────────────────────── */}
          {jobs.length > 0 && (
            <div style={styles.section}>
              <div style={styles.sectionTitle}>Scheduled Jobs</div>
              <table style={styles.table}>
                <thead>
                  <tr>
                    <th style={styles.th}>Job</th>
                    <th style={styles.th}>Next Run</th>
                    <th style={styles.th}>Trigger</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.id}>
                      <td style={styles.td}>{j.id}</td>
                      <td style={styles.td}>
                        {j.next_run_time
                          ? new Date(j.next_run_time).toLocaleString()
                          : "—"}
                      </td>
                      <td style={styles.td}>{j.trigger}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* ── Research schedule ───────────────────────────────────── */}
          {research.length > 0 && (
            <div style={styles.section}>
              <div style={styles.sectionTitle}>Research Retries</div>
              <table style={styles.table}>
                <thead>
                  <tr>
                    <th style={styles.th}>Task</th>
                    <th style={styles.th}>Query</th>
                    <th style={styles.th}>Next Attempt</th>
                    <th style={styles.th}>Last Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {research.map((r) => (
                    <tr key={r.task_id}>
                      <td style={styles.td}>{r.title}</td>
                      <td style={{ ...styles.td, maxWidth: "180px" }}>
                        {r.query || "—"}
                      </td>
                      <td style={styles.td}>
                        {r.next_attempt_at
                          ? new Date(r.next_attempt_at).toLocaleString()
                          : "every sweep"}
                      </td>
                      <td style={styles.td}>{r.last_outcome || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {jobs.length === 0 && research.length === 0 && (
            <div style={styles.empty}>No jobs or research retries.</div>
          )}
        </>
      )}
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
  heading: {
    fontWeight: 600,
    opacity: 0.7,
    cursor: "pointer",
    userSelect: "none",
  },
  section: { marginTop: "0.5rem" },
  sectionTitle: {
    fontWeight: 600,
    fontSize: "0.72rem",
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    opacity: 0.6,
    marginBottom: "0.3rem",
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: "0.76rem",
  },
  th: {
    textAlign: "left",
    padding: "0.2rem 0.4rem",
    borderBottom: "1px solid rgba(0,0,0,0.1)",
    fontWeight: 600,
    opacity: 0.7,
  },
  td: {
    padding: "0.2rem 0.4rem",
    borderBottom: "1px solid rgba(0,0,0,0.05)",
    wordBreak: "break-word",
  },
  empty: {
    opacity: 0.5,
    padding: "0.3rem 0",
    fontStyle: "italic",
  },
};
