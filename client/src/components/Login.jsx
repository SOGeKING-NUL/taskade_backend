import { useAuth0 } from "@auth0/auth0-react";

export default function Login() {
  const { loginWithRedirect, error } = useAuth0();

  const signInWithGoogle = () => {
    loginWithRedirect({ authorizationParams: { connection: "google-oauth2" } });
  };

  return (
    <div className="app launch-screen">
      <div className="launch-content">
        <h1 className="title" style={{ marginTop: "16px" }}>Voice AI</h1>
        <p className="subtitle" style={{ marginBottom: "32px" }}>
          Sign in to continue
        </p>
        <button className="launch-btn" onClick={signInWithGoogle}>
          Sign in with Google
        </button>
        {error && <p className="launch-hint" style={{ color: "#f87171" }}>{error.message}</p>}
      </div>
    </div>
  );
}
