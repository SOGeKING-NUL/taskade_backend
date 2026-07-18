/**
 * TaskRow — one task card for the Tasks grid: status, title, due date,
 * research links, nested sub-tasks (if it's a milestone), and status actions
 * (Done / Cancel / Reopen) via PATCH /tasks/{id}.
 */
const STATUS_LABEL = {
  pending: "Pending",
  active: "Active",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
};

function isClosed(status) {
  return status === "done" || status === "cancelled";
}

function fmtDue(due) {
  if (!due) return null;
  const d = new Date(due);
  return (
    d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) +
    ", " +
    d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
  );
}

function linkHref(l) {
  return typeof l === "string" ? l : l?.url;
}
function linkLabel(l) {
  return typeof l === "string" ? l : l?.label || l?.url;
}

export default function TaskRow({ task, subtasks = [], onSetStatus }) {
  const closed = isClosed(task.status);
  const links = task.context?.research?.links || [];
  const doneCount = subtasks.filter((s) => s.status === "done").length;

  return (
    <div className={`task-card ${closed ? "closed" : ""}`}>
      <span className={`task-status s-${task.status}`}>
        {STATUS_LABEL[task.status] || task.status}
      </span>
      <div className="task-title">{task.title}</div>
      {fmtDue(task.due_at) && <div className="task-sub">Due {fmtDue(task.due_at)}</div>}
      {subtasks.length > 0 && (
        <div className="task-sub">
          {doneCount}/{subtasks.length} done
        </div>
      )}
      {links.map((l, j) => {
        const url = linkHref(l);
        if (!url) return null;
        return (
          <a key={j} className="task-link" href={url} target="_blank" rel="noreferrer">
            {linkLabel(l)}
          </a>
        );
      })}

      {subtasks.length > 0 && (
        <div className="task-children">
          {subtasks.map((s) => {
            const subClosed = isClosed(s.status);
            return (
              <div key={s.id} className={`task-child ${subClosed ? "closed" : ""}`}>
                <span className={`task-status s-${s.status}`}>
                  {STATUS_LABEL[s.status] || s.status}
                </span>
                <span className="task-child-title">{s.title}</span>
                {onSetStatus && !subClosed && (
                  <button
                    className="task-mini-btn"
                    onClick={() => onSetStatus(s.id, "done")}
                    title="Mark done"
                    aria-label={`Mark "${s.title}" done`}
                  >
                    ✓
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {onSetStatus && (
        <div className="task-actions">
          {!closed && (
            <>
              <button className="task-btn done" onClick={() => onSetStatus(task.id, "done")}>
                Done
              </button>
              <button
                className="task-btn cancel"
                onClick={() => onSetStatus(task.id, "cancelled")}
              >
                Cancel
              </button>
            </>
          )}
          {closed && (
            <button className="task-btn reopen" onClick={() => onSetStatus(task.id, "pending")}>
              Reopen
            </button>
          )}
        </div>
      )}
    </div>
  );
}
