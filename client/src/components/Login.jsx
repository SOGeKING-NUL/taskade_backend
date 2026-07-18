/**
 * Login — single Google sign-in gate. The WS can't connect without an Auth0
 * ID token, so nothing else mounts until the user is authenticated.
 */
import { useAuth0 } from "@auth0/auth0-react";

export default function Login() {
  const { loginWithRedirect } = useAuth0();

  return (
    <div className="app login-screen">
      <div className="panel-content">
        <h1 className="brand">TASKADE</h1>
        <p className="brand-sub">Your voice, doing things.</p>
        <button
          className="btn-primary"
          onClick={() =>
            loginWithRedirect({ authorizationParams: { connection: "google-oauth2" } })
          }
        >
          Sign in with Google
        </button>
        <p className="fine-print">Microphone access is requested after sign-in.</p>
      </div>
    </div>
  );
}
