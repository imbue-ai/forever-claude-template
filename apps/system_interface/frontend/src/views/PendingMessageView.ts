/**
 * Renders optimistic ("pending") message bubbles -- messages the user just sent
 * that have not yet been reconciled against a real transcript event.
 *
 * Lives in its own module (rather than inline in ChatPanel) so it can be unit
 * tested against the real ``renderUserMessage`` without pulling in ChatPanel's
 * heavy dockview/streaming module graph.
 */

import m from "mithril";
import {
  getPendingMessages,
  getPendingMessage,
  markPendingMessageQueued,
  markPendingMessageSending,
  removePendingMessage,
} from "../models/PendingMessages";
import { interruptAgent, sendMessage } from "../models/Response";
import { describeRequestError } from "../models/request-error";
import { renderUserMessage } from "./message-renderers";

/**
 * Interrupt the agent and re-send a queued message.
 *
 * A message shown as "queued" has been accepted into the agent's queue but not
 * yet processed (the agent is busy). Interrupting forces the agent idle so the
 * message is handled now -- but the interrupt clears the agent's queue, so the
 * message must be re-sent afterward. Conversation history is preserved (the
 * interrupt resumes the same session). On failure the bubble is rolled back and
 * the error surfaced.
 */
export async function interruptAndResend(agentId: string, id: string): Promise<void> {
  const pending = getPendingMessage(agentId, id);
  if (pending === undefined) {
    return;
  }
  const text = pending.content;
  // Back to "sending" -- this also hides the action button so it can't double-fire.
  markPendingMessageSending(agentId, id);
  m.redraw();
  try {
    await interruptAgent(agentId);
    await sendMessage(agentId, text);
    markPendingMessageQueued(agentId, id);
  } catch (err) {
    const detail = describeRequestError(err);
    console.error(`Failed to interrupt and send to agent ${agentId}: ${detail}`);
    removePendingMessage(agentId, id);
    alert(`Failed to interrupt and send: ${detail}`);
  }
}

/** The status row shown beneath a pending bubble, keyed so it can sit alongside
 *  the keyed bubble vnode (Mithril requires all-or-no keys among siblings). */
function renderStatusRow(agentId: string, id: string, status: "sending" | "queued"): m.Vnode {
  if (status === "sending") {
    return m("div", { key: `pending-status-${id}`, class: "pending-message-status" }, "Sending…");
  }
  // queued: accepted into the agent's queue, awaiting processing -- offer an
  // icon action to interrupt the agent and send it now.
  return m("div", { key: `pending-status-${id}`, class: "pending-message-status" }, [
    m("span", "Queued"),
    m(
      "button",
      {
        type: "button",
        class: "pending-message-interrupt",
        // CSS tooltip (native `title` is unreliable in the webview) -- see the
        // data-tooltip pattern shared with the progress-view markers.
        "data-tooltip": "Interrupt the agent and send now",
        "aria-label": "Interrupt and send now",
        onclick: () => interruptAndResend(agentId, id),
      },
      // Up-arrow ("send") icon, matching the message-input send button.
      m.trust(
        '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
      ),
    ),
  ]);
}

/**
 * Optimistic bubbles for messages the user just sent. Rendered with the same
 * renderer as real user turns so they are visually indistinguishable, and meant
 * to be appended after the transcript so they sit at the bottom where the user
 * expects their message. A "sending" bubble is dimmed with a "Sending…" caption;
 * a "queued" bubble shows a "Queued" caption plus an "Interrupt and send"
 * action. Either way it stays up until the real transcript event reconciles it
 * away (the agent genuinely received it).
 */
export function renderPendingMessages(agentId: string): m.Vnode[] {
  const nodes: m.Vnode[] = [];
  for (const pending of getPendingMessages(agentId)) {
    const bubble = renderUserMessage({
      type: "user_message",
      event_id: pending.id,
      content: pending.content,
      role: "user",
      source: "pending",
      timestamp: "",
    });
    if (bubble === null) continue;
    const isSending = pending.status === "sending";
    // renderUserMessage returns a keyed vnode, so every sibling in this wrapper
    // must also be keyed: Mithril throws if a children array mixes keyed and
    // unkeyed vnodes (or contains a null hole alongside keyed nodes). Build the
    // children imperatively so the array is always fully keyed with no holes.
    const children: m.Vnode[] = [bubble, renderStatusRow(agentId, pending.id, pending.status)];
    nodes.push(
      m(
        "div",
        {
          key: `pending-wrap-${pending.id}`,
          class: isSending ? "pending-message pending-message--sending" : "pending-message pending-message--queued",
        },
        children,
      ),
    );
  }
  return nodes;
}
