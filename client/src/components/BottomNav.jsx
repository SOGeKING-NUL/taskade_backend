/**
 * BottomNav — switches between the Talk (orb) view and the Tasks page. Always
 * visible so the pages are discoverable. Switching away from Talk mutes the
 * bot (handled in App via the `active` prop) while keeping the WS alive.
 */
const TABS = [
  {
    id: "talk",
    label: "Talk",
    icon: (
      <>
        <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
        <path d="M19 10a7 7 0 0 1-14 0" />
        <path d="M12 19v3" />
      </>
    ),
  },
  {
    id: "tasks",
    label: "Tasks",
    icon: (
      <>
        <path d="M8 6h13" />
        <path d="M8 12h13" />
        <path d="M8 18h13" />
        <path d="M3 6h.01" />
        <path d="M3 12h.01" />
        <path d="M3 18h.01" />
      </>
    ),
  },
];

export default function BottomNav({ view, onChange }) {
  return (
    <nav className="bottom-nav">
      {TABS.map((t) => (
        <button
          key={t.id}
          className={`nav-tab ${view === t.id ? "on" : ""}`}
          onClick={() => onChange(t.id)}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {t.icon}
          </svg>
          <span>{t.label}</span>
        </button>
      ))}
    </nav>
  );
}
