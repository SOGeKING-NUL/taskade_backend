import { useState, useEffect, useCallback, useMemo } from "react";
import { useAuth0 } from "@auth0/auth0-react";
import Login from "./components/Login";
import Onboarding from "./components/Onboarding";
import VoiceChat from "./components/VoiceChat";
import TasksPage from "./components/TasksPage";
import BottomNav from "./components/BottomNav";
import { api } from "./utils/api";

function App() {
  const {
    isLoading,
    isAuthenticated,
    getAccessTokenSilently,
    getIdTokenClaims,
    logout,
    user,
  } = useAuth0();

  const [idToken, setIdToken] = useState(null);
  const [profile, setProfile] = useState(null); // null=loading | object | "error"
  const [greeting, setGreeting] = useState("");
  const [launched, setLaunched] = useState(false);
  const [view, setView] = useState("talk"); // "talk" | "tasks"
  const [tasks, setTasks] = useState([]);

  // Authenticated → exercise refresh renewal, then grab the raw ID token.
  useEffect(() => {
    if (!isAuthenticated) return;
    let cancelled = false;
    (async () => {
      try {
        await getAccessTokenSilently();
        const claims = await getIdTokenClaims();
        if (!cancelled) setIdToken(claims?.__raw ?? null);
      } catch (err) {
        console.error("Token retrieval failed:", err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, getAccessTokenSilently, getIdTokenClaims]);

  // Token ready → load the profile (onboarding gate) and the app-open greeting.
  const loadProfile = useCallback(() => {
    if (!idToken) return;
    const rest = api(idToken);
    rest
      .getProfile()
      .then((d) => setProfile(d.profile || {}))
      .catch(() => setProfile("error")); // a failed load must NOT masquerade as "not onboarded"
    rest
      .getGreeting()
      .then((d) => setGreeting(d.greeting || ""))
      .catch(() => {});
  }, [idToken]);

  useEffect(() => {
    loadProfile();
  }, [loadProfile]);

  // Tasks live here, ABOVE the "talk"/"tasks" view toggle, so switching views
  // never refetches — the list persists and is only re-pulled when something
  // actually changed (a tool result) or the user hits refresh.
  const rest = useMemo(() => (idToken ? api(idToken) : null), [idToken]);

  const refreshTasks = useCallback(() => {
    if (!rest) return;
    rest
      .getTasks()
      .then((d) => setTasks(d.tasks || []))
      .catch((err) => console.warn("Failed to load tasks:", err));
  }, [rest]);

  const setTaskStatus = useCallback(
    async (id, status) => {
      if (!rest) return;
      try {
        await rest.setTaskStatus(id, status);
        refreshTasks();
      } catch (err) {
        console.warn("Failed to update task status:", err);
      }
    },
    [rest, refreshTasks]
  );

  // Fetch once we reach the main app (post-onboarding) — not on every nav tap.
  useEffect(() => {
    if (profile && profile !== "error" && profile.onboarding_complete) {
      refreshTasks();
    }
  }, [profile, refreshTasks]);

  if (isLoading) return <div className="app" />;
  if (!isAuthenticated) return <Login />;
  if (!idToken || profile === null) return <div className="app" />; // fetching token/profile

  if (profile === "error") {
    return (
      <div className="app launch-screen">
        <div className="panel-content">
          <h1 className="brand">HMM</h1>
          <p className="brand-sub">Couldn't reach the server. Is the backend running?</p>
          <button
            className="btn-primary"
            onClick={() => {
              setProfile(null);
              loadProfile();
            }}
          >
            Retry
          </button>
          <button
            className="btn-ghost"
            onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  if (!profile.onboarding_complete) {
    return <Onboarding token={idToken} initialName={user?.name} onDone={setProfile} />;
  }

  if (!launched) {
    return (
      <div className="app launch-screen">
        <div className="panel-content">
          <h1 className="brand">TASKADE</h1>
          {greeting ? (
            <p className="greeting">{greeting}</p>
          ) : (
            <p className="brand-sub">Natural, hands-free voice conversation</p>
          )}
          <button className="btn-primary" onClick={() => setLaunched(true)}>
            Start Conversation
          </button>
          <p className="fine-print">Microphone access will be requested</p>
          <button
            className="btn-ghost"
            onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  // Persistent shell: VoiceChat stays mounted (WS + mic alive, just dimmed and
  // muted) across views; the Tasks page overlays it translucently on top, and
  // leaving "talk" mutes the bot without dropping the connection.
  return (
    <div className="app shell">
      <div className="shell-content">
        <VoiceChat
          accessToken={idToken}
          active={view === "talk"}
          onEnd={() => setLaunched(false)}
          onTaskChange={refreshTasks}
        />
        {view === "tasks" && (
          <TasksPage tasks={tasks} onRefresh={refreshTasks} onSetStatus={setTaskStatus} />
        )}
      </div>
      <BottomNav view={view} onChange={setView} />
    </div>
  );
}

export default App;
