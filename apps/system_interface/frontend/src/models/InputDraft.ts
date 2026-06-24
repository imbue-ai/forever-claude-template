/**
 * One-shot "prefill the composer" channel, keyed by agentId.
 *
 * A choice card rendered inside the transcript (see views/choice-cards.ts) lives
 * in a different part of the component tree from the MessageInput composer, so it
 * can't set the input's text directly. Instead it writes a pending draft here;
 * the MessageInput for that agent consumes it on the next redraw, dropping the
 * text into the box and focusing it. This is *prefill only* -- it never sends, so
 * the user can edit the text before hitting enter.
 *
 * Mirrors the module-level store pattern used by PendingMessages.ts (plain map +
 * an explicit redraw) rather than introducing any new state mechanism.
 */

import m from "mithril";

// Pending prefill text per agent. Empty string is a meaningful value -- it means
// "clear the box and focus it" (e.g. the "I have something in mind" card) -- so
// presence is tracked by Map membership, not by truthiness.
const pendingDraftByAgentId = new Map<string, string>();

/** Queue `text` to be dropped into `agentId`'s composer on the next redraw. */
export function setInputDraft(agentId: string, text: string): void {
  pendingDraftByAgentId.set(agentId, text);
  // Nudge a redraw so the composer picks the draft up even when this is called
  // from a non-DOM context; from a DOM event handler mithril would redraw anyway.
  m.redraw();
}

/**
 * Take and clear the pending draft for `agentId`. Returns the queued string
 * (possibly empty) when one was pending, or null when there was nothing queued.
 * The empty-vs-null distinction is load-bearing: an empty draft still focuses
 * and clears the box, whereas null leaves the composer untouched.
 */
export function consumeInputDraft(agentId: string): string | null {
  if (!pendingDraftByAgentId.has(agentId)) {
    return null;
  }
  const text = pendingDraftByAgentId.get(agentId) ?? "";
  pendingDraftByAgentId.delete(agentId);
  return text;
}
