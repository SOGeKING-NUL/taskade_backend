/**
 * stripMarkdown — best-effort plain-text cleanup for LLM-generated prose that's
 * displayed in a card (not spoken). Research findings come from a separate,
 * less-constrained model and sometimes carry markdown syntax (bold, inline
 * links) even though the actual links are already shown as separate chips —
 * this strips the syntax so the text reads as plain prose instead of literal
 * asterisks/brackets.
 */
export function stripMarkdown(text) {
  if (!text) return text;
  return text
    // [label](url) -> label (the url is already shown separately as a link chip)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1")
    // **bold** / __bold__ -> bold
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    // remaining single */_ emphasis markers
    .replace(/\*([^*]+)\*/g, "$1")
    .trim();
}
