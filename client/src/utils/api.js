/**
 * api — one place for all authenticated REST calls to the backend. Every
 * component gets its calls from here (via `api(token)`) so auth headers and the
 * base URL live in exactly one module.
 */
const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

export function api(token) {
  const auth = { Authorization: `Bearer ${token}` };
  const authJson = { ...auth, "Content-Type": "application/json" };
  // Throw on non-2xx so callers' .catch() fires — a failed /profile must NOT be
  // mistaken for an empty profile (which would wrongly re-trigger onboarding).
  const json = (r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  };

  return {
    getProfile: () => fetch(`${API_BASE}/profile`, { headers: auth }).then(json),
    // NOTE: consuming endpoint — calling it marks the reminders delivered.
    getReminders: () => fetch(`${API_BASE}/reminders/due`, { headers: auth }).then(json),
    getGreeting: () => fetch(`${API_BASE}/engagement/greeting`, { headers: auth }).then(json),
    getTasks: () => fetch(`${API_BASE}/tasks`, { headers: auth }).then(json),
    // Status changes (done/cancelled/reopen) all go through PATCH; the backend
    // also exposes DELETE /tasks/{id} as an equivalent soft-cancel, unused here.
    setTaskStatus: (id, status) =>
      fetch(`${API_BASE}/tasks/${id}`, {
        method: "PATCH",
        headers: authJson,
        body: JSON.stringify({ status }),
      }).then(json),
    completeOnboarding: (body) =>
      fetch(`${API_BASE}/profile/onboarding`, {
        method: "POST",
        headers: authJson,
        body: JSON.stringify(body),
      }).then(json),
    setLocation: (location, timezone) =>
      fetch(`${API_BASE}/profile/location`, {
        method: "POST",
        headers: authJson,
        body: JSON.stringify({ location, timezone }),
      }).then(json),
  };
}

/**
 * reverseGeocode — browser geolocation → "City, Country" via a keyless service.
 * Resolves to a string, or rejects (caller can fall back to manual entry).
 */
export function reverseGeocode() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("no geolocation"));
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        try {
          const { latitude, longitude } = pos.coords;
          const r = await fetch(
            `https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${latitude}&longitude=${longitude}&localityLanguage=en`
          );
          const g = await r.json();
          const city = g.city || g.locality || g.principalSubdivision;
          resolve([city, g.countryName].filter(Boolean).join(", "));
        } catch (e) {
          reject(e);
        }
      },
      reject,
      { timeout: 10000, maximumAge: 86400000 }
    );
  });
}

export { API_BASE };
