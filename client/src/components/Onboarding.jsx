/**
 * Onboarding — first-run capture (name, optional location, daily check-in time).
 * Gated on the profile's `onboarding_complete`; POSTs to /profile/onboarding.
 * Timezone is auto-detected from the browser so the check-in hour is local.
 */
import { useState } from "react";
import { api, reverseGeocode } from "../utils/api";

const CHECKIN_PRESETS = [
  { label: "Morning", hour: 7 },
  { label: "Afternoon", hour: 13 },
  { label: "Evening", hour: 18 },
  { label: "Night", hour: 21 },
];

export default function Onboarding({ token, initialName, onDone }) {
  const [name, setName] = useState(initialName || "");
  const [location, setLocation] = useState("");
  const [hour, setHour] = useState(7);
  const [busy, setBusy] = useState(false);
  const [locating, setLocating] = useState(false);

  const detect = async () => {
    setLocating(true);
    try {
      const loc = await reverseGeocode();
      if (loc) setLocation(loc);
    } catch {
      /* ignore — the user can type it manually */
    } finally {
      setLocating(false);
    }
  };

  const submit = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      const data = await api(token).completeOnboarding({
        display_name: name.trim(),
        location: location.trim() || null,
        timezone: tz || null,
        daily_checkin_hour: hour,
      });
      onDone(data.profile || { onboarding_complete: true });
    } catch (err) {
      console.error("Onboarding failed:", err);
      setBusy(false);
    }
  };

  return (
    <div className="app onboarding-screen">
      <div className="panel-content form">
        <h1 className="brand">HELLO</h1>
        <p className="brand-sub">A few things so I can help properly.</p>

        <label className="field">
          <span className="field-label">What should I call you?</span>
          <input
            className="field-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Your name"
          />
        </label>

        <label className="field">
          <span className="field-label">
            Where are you based? <em>(optional)</em>
          </span>
          <div className="field-row">
            <input
              className="field-input"
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              placeholder="City, Country"
            />
            <button
              type="button"
              className="btn-ghost"
              onClick={detect}
              disabled={locating}
            >
              {locating ? "…" : "Detect"}
            </button>
          </div>
        </label>

        <div className="field">
          <span className="field-label">Daily check-in</span>
          <div className="chip-row">
            {CHECKIN_PRESETS.map((p) => (
              <button
                type="button"
                key={p.hour}
                className={`chip-select ${hour === p.hour ? "on" : ""}`}
                onClick={() => setHour(p.hour)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <button className="btn-primary" onClick={submit} disabled={!name.trim() || busy}>
          {busy ? "Saving…" : "Get Started"}
        </button>
      </div>
    </div>
  );
}
