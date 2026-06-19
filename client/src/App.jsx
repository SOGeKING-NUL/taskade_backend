import { useState, useEffect } from "react";
import { useAuth0 } from "@auth0/auth0-react";
import VoiceChat from "./components/VoiceChat";
import Login from "./components/Login";

function App() {
  const { isLoading, isAuthenticated, getIdTokenClaims, logout } = useAuth0();
  const [idToken, setIdToken] = useState(null);
  const [launched, setLaunched] = useState(false);

  useEffect(() => {
    if (!isAuthenticated) {
      setIdToken(null);
      return;
    }
    getIdTokenClaims().then((claims) => setIdToken(claims?.__raw || null));
  }, [isAuthenticated, getIdTokenClaims]);

  if (isLoading) {
    return <div className="app launch-screen" />; // brief auth check, avoid login flash
  }

  if (!isAuthenticated) {
    return <Login />;
  }

  if (!launched) {
    return (
      <div className="app launch-screen">
        <div className="launch-content">
          <h1 className="title" style={{ marginTop: "16px" }}>Voice AI</h1>
          <p className="subtitle" style={{ marginBottom: "32px" }}>
            Natural, hands-free voice conversation
          </p>
          <button
            className="launch-btn"
            disabled={!idToken}
            onClick={() => {
              console.log("Start Conversation clicked, mounting VoiceChat...");
              setLaunched(true);
            }}
          >
            Start Conversation
          </button>
          <p className="launch-hint">
            Microphone access will be requested
          </p>
          <button
            className="clear-btn"
            style={{ marginTop: "24px" }}
            onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <VoiceChat accessToken={idToken} />
    </div>
  );
}

export default App;
