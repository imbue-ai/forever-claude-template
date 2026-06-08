/**
 * Renders optimistic ("pending") message bubbles -- messages the user just sent
 * that have not yet been reconciled against a real transcript event.
 *
 * Lives in its own module (rather than inline in ChatPanel) so it can be unit
 * tested against the real ``renderUserMessage`` without pulling in ChatPanel's
 * heavy dockview/streaming module graph.
 */

import m from "mithril";
import { getPendingMessages } from "../models/PendingMessages";
import { renderUserMessage } from "./message-renderers";

/**
 * Optimistic bubbles for messages the user just sent. Rendered with the same
 * renderer as real user turns so they are visually indistinguishable, and meant
 * to be appended after the transcript so they sit at the bottom where the user
 * expects their message. While the send request is still in flight the bubble
 * carries a subtle "sending" affordance (dimmed, with a small caption); once the
 * request resolves it settles to a normal bubble, until the real transcript
 * event reconciles it away.
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
    const children: m.Vnode[] = [bubble];
    if (isSending) {
      children.push(m("div", { key: `pending-status-${pending.id}`, class: "pending-message-status" }, "Sending…"));
    }
    nodes.push(
      m(
        "div",
        {
          key: `pending-wrap-${pending.id}`,
          class: isSending ? "pending-message pending-message--sending" : "pending-message",
        },
        children,
      ),
    );
  }
  return nodes;
}
