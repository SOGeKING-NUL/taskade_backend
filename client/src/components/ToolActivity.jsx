/**
 * ToolActivity — ephemeral, one-line indicator of what the assistant is doing
 * this turn (tool.start / tool.result). Cleared when a new user turn begins.
 */
const LABEL = {
  create_task: "task",
  update_task: "task",
  query_tasks: "looking up tasks",
  research: "searching the web",
  update_profile: "profile",
};

export default function ToolActivity({ items }) {
  if (!items.length) return null;
  return (
    <div className="tool-activity">
      {items.map((it, i) => (
        <span
          key={i}
          className={`tool-chip ${it.kind}${it.ok === false ? " fail" : ""}`}
        >
          {it.kind === "start"
            ? `${LABEL[it.name] || it.name}…`
            : `${it.ok === false ? "✕" : "✓"} ${LABEL[it.name] || it.name}`}
        </span>
      ))}
    </div>
  );
}
