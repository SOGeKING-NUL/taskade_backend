import { useState } from "react";
import VoiceChat from "./components/VoiceChat";

function App() {
  const [launched, setLaunched] = useState(false);

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
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <VoiceChat />
    </div>
  );
}

export default App;
