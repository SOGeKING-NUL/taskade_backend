/**
 * TasksPage — translucent overlay listing the user's tasks as a grid of cards.
 * Purely presentational: `tasks` is owned by the App shell (fetched once,
 * refreshed automatically when a voice turn changes something, or manually
 * via the refresh button) so switching in/out of this view never refetches.
 * Sub-tasks are nested inside their parent's card instead of a flat list.
 */
import TaskRow from "./TaskRow";

export default function TasksPage({ tasks, onRefresh, onSetStatus }) {
  // A task counts as a "child" here only if its parent is also in the list
  // (a parent already filtered out — e.g. cancelled — falls back to standalone).
  const ids = new Set(tasks.map((t) => t.id));
  const childrenByParent = {};
  for (const t of tasks) {
    if (t.parent_id && ids.has(t.parent_id)) {
      (childrenByParent[t.parent_id] ||= []).push(t);
    }
  }
  const topLevel = tasks.filter((t) => !t.parent_id || !ids.has(t.parent_id));
  const open = topLevel.filter((t) => t.status !== "done" && t.status !== "cancelled");
  const closed = topLevel.filter((t) => t.status === "done" || t.status === "cancelled");

  return (
    <div className="page">
      <div className="page-head">
        <span className="page-title">Tasks</span>
        <button className="page-refresh" onClick={onRefresh} aria-label="Refresh" title="Refresh">
          ↻
        </button>
      </div>
      <div className="page-body">
        {tasks.length === 0 && (
          <p className="page-empty">No tasks yet — talk to me and I'll track things for you.</p>
        )}
        {open.length > 0 && (
          <div className="tasks-grid">
            {open.map((t) => (
              <TaskRow
                key={t.id}
                task={t}
                subtasks={childrenByParent[t.id]}
                onSetStatus={onSetStatus}
              />
            ))}
          </div>
        )}
        {closed.length > 0 && (
          <>
            <div className="page-section">Done</div>
            <div className="tasks-grid">
              {closed.map((t) => (
                <TaskRow
                  key={t.id}
                  task={t}
                  subtasks={childrenByParent[t.id]}
                  onSetStatus={onSetStatus}
                />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
