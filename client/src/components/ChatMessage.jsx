/**
 * ChatMessage — single message bubble (user or assistant).
 */
export default function ChatMessage({ message, streaming = false }) {
  return (
    <div
      className={`message ${message.role}${streaming ? " streaming" : ""}${
        message.error ? " error" : ""
      }`}
    >
      <span className="message-role">
        {message.role === "user" ? "You" : "AI"}
      </span>
      <p className="message-text">{message.text}</p>
    </div>
  );
}
