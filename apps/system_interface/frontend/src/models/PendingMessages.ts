/**
 * Optimistic, client-side store for messages the user has just sent.
 *
 * The chat view renders only events read back from the agent's session
 * transcript, so a freshly-sent message is invisible until the backend writes
 * it to the transcript file and re-broadcasts it. That round trip takes a
 * couple of seconds when the agent is idle, and -- far worse -- until the
 * *current turn finishes* when the agent is mid-run, because Claude Code queues
 * a message received while it is working and only writes it to the transcript
 * once the running turn completes. The user perceives the latter as the message
 * being dropped entirely.
 *
 * This store closes that gap: a sent message is held here and rendered
 * immediately as a normal user bubble, then reconciled away once its real
 * transcript event arrives (matched by content). If that real event is delayed
 * for minutes (the mid-run case) the optimistic bubble simply stays up the
 * whole time, so the message never appears to vanish.
 *
 * It also drives a forced "Thinking..." activity indicator: when the user sends
 * to an idle agent, the indicator would otherwise stay blank until the backend
 * recomputes activity from the new transcript tail. While such a send is still
 * unreconciled we report THINKING locally -- but only for a send made while the
 * agent was genuinely idle; a message sent to an already-working agent leaves
 * its real activity untouched. Tying the override to the pending message's own
 * lifetime (rather than a separate flag cleared by a later backend signal) means
 * it can never get stuck on: the moment the real transcript event lands, the
 * override and the bubble clear together and the backend's state takes over.
 */

import m from "mithril";
import { getAgentById } from "./AgentManager";
import type { TranscriptEvent } from "./Response";

/**
 * Lifecycle status of an optimistic message, driven by the send POST.
 * "sending" while the POST is in flight (not yet known to be accepted);
 * "queued" once it resolves successfully -- the backend confirms the message
 * was accepted into the agent's queue (its enqueue event), so it WILL be
 * received, but may not have been processed yet. The bubble stays up in either
 * state until the real transcript event arrives (the agent genuinely received
 * it), at which point reconciliation removes it -- that is the user-facing
 * "sent". A failed send never reaches "queued"; it is rolled back via
 * removePendingMessage instead.
 */
export type PendingMessageStatus = "sending" | "queued";

export interface PendingMessage {
  /** Stable id for keying the rendered bubble. */
  id: string;
  /** Trimmed message text, matched against transcript user_message content. */
  content: string;
  /** Delivery status, used to render a subtle "sending" affordance until the
   *  send request confirms the agent received the message. */
  status: PendingMessageStatus;
  /** True when the agent was IDLE at send time, so this message should force a
   *  "Thinking..." indicator until it reconciles. False for a message sent to an
   *  already-working agent (its real activity is shown unchanged). */
  sent_while_idle: boolean;
  /** event_ids of user_message events already in the transcript when this
   *  message was sent. Reconciliation ignores these so an older, identical
   *  message can never spuriously "claim" (and hide) this pending one. */
  prior_user_event_ids: Set<string>;
}

let nextPendingId = 0;

const pendingByAgent: Record<string, PendingMessage[]> = {};

function userEventIds(events: readonly TranscriptEvent[]): Set<string> {
  const ids = new Set<string>();
  for (const event of events) {
    if (event.type === "user_message") {
      ids.add(event.event_id);
    }
  }
  return ids;
}

/**
 * Record a just-sent message so it renders immediately. ``currentEvents`` is the
 * agent's transcript at send time, used both to snapshot which user messages
 * already exist and (via the live agent state) to decide whether to force a
 * "Thinking..." indicator while the send is in flight.
 *
 * Returns the id of the created pending message so the caller can roll it back
 * (via ``removePendingMessage``) if the send ultimately fails -- otherwise the
 * optimistic bubble, and any forced-THINKING override it triggered, would stay
 * up forever since no real transcript event will ever arrive to reconcile it.
 * Returns ``null`` when the trimmed content is blank and nothing was added.
 */
export function addPendingMessage(
  agentId: string,
  content: string,
  currentEvents: readonly TranscriptEvent[],
): string | null {
  const trimmed = content.trim();
  if (!trimmed) {
    return null;
  }
  // "force ... if (and ONLY IF) it's totally idle": a working agent already
  // surfaces its own activity, and a null state means activity isn't tracked
  // for this agent at all, so neither should be overridden.
  const sentWhileIdle = getAgentById(agentId)?.activity_state === "IDLE";
  const id = `pending-${nextPendingId++}`;
  const list = pendingByAgent[agentId] ?? [];
  list.push({
    id,
    content: trimmed,
    status: "sending",
    sent_while_idle: sentWhileIdle,
    prior_user_event_ids: userEventIds(currentEvents),
  });
  pendingByAgent[agentId] = list;
  m.redraw();
  return id;
}

/** The pending message with this id, or undefined. */
export function getPendingMessage(agentId: string, id: string): PendingMessage | undefined {
  return pendingByAgent[agentId]?.find((p) => p.id === id);
}

/**
 * Mark a pending message as queued once its send request resolves successfully:
 * the backend has confirmed the agent accepted it into its queue. The bubble
 * stays up (still optimistic) until the real transcript event reconciles it
 * away -- that, not this, is when the user sees it as "sent". A queued message
 * is the one offered the "interrupt and send" action. Marking an unknown id is
 * a no-op.
 */
export function markPendingMessageQueued(agentId: string, id: string): void {
  const pending = pendingByAgent[agentId]?.find((p) => p.id === id);
  if (pending !== undefined && pending.status !== "queued") {
    pending.status = "queued";
    m.redraw();
  }
}

/**
 * Put a pending message back into the "sending" state -- used when re-sending it
 * (e.g. "interrupt and send", which interrupts the agent and resends, since the
 * interrupt clears Claude's queue). Setting an unknown id is a no-op.
 */
export function markPendingMessageSending(agentId: string, id: string): void {
  const pending = pendingByAgent[agentId]?.find((p) => p.id === id);
  if (pending !== undefined && pending.status !== "sending") {
    pending.status = "sending";
    m.redraw();
  }
}

/**
 * Remove a single optimistic message by id, clearing its bubble and any
 * forced-THINKING override it triggered. Used to roll back a pending message
 * whose send failed (so no real transcript event will ever reconcile it).
 * Removing an unknown id is a no-op.
 */
export function removePendingMessage(agentId: string, id: string): void {
  const list = pendingByAgent[agentId];
  if (list === undefined) {
    return;
  }
  const remaining = list.filter((pending) => pending.id !== id);
  if (remaining.length !== list.length) {
    pendingByAgent[agentId] = remaining;
    m.redraw();
  }
}

/** The still-unreconciled optimistic messages for an agent, in send order. */
export function getPendingMessages(agentId: string): PendingMessage[] {
  return pendingByAgent[agentId] ?? [];
}

/**
 * Drop optimistic messages whose real transcript event has now arrived.
 *
 * Each pending message is matched to the earliest user_message event that (a)
 * was not already present when the message was sent and (b) has not been
 * claimed by an earlier pending message, so two identical sends reconcile
 * against two distinct transcript events rather than collapsing into one.
 *
 * Matching is by trimmed content: the send POST confirms delivery but returns
 * no server-assigned id to correlate on, and the backend persists the user's
 * text verbatim into the transcript, so equality holds
 * modulo the surrounding whitespace both sides trim. That verbatim-persistence
 * is the contract this relies on; if the backend ever rewrote user text the
 * bubble would not reconcile. (A server-returned correlation id would remove
 * that fragility, at the cost of a backend change -- a worthwhile follow-up.)
 *
 * Matching ignores the frontend's hidden-message classification (skill
 * expansions, /welcome, stop-hook feedback). That is safe because those are
 * hook/system texts a human never types, so a user-authored pending message can
 * never content-match one.
 */
export function reconcilePendingMessages(agentId: string, events: readonly TranscriptEvent[]): void {
  const list = pendingByAgent[agentId];
  if (list === undefined || list.length === 0) {
    return;
  }
  const claimed = new Set<string>();
  const remaining: PendingMessage[] = [];
  for (const pending of list) {
    const match = events.find(
      (event) =>
        event.type === "user_message" &&
        !pending.prior_user_event_ids.has(event.event_id) &&
        !claimed.has(event.event_id) &&
        event.content.trim() === pending.content,
    );
    if (match !== undefined) {
      claimed.add(match.event_id);
    } else {
      remaining.push(pending);
    }
  }
  if (remaining.length !== list.length) {
    pendingByAgent[agentId] = remaining;
  }
}

/**
 * The activity state to display for an agent, applying the local
 * forced-THINKING override. Real work always wins; otherwise an unreconciled
 * idle-send upgrades an IDLE agent to THINKING. Any other real state (a tracked
 * working state, or an untracked ``null``) is returned unchanged.
 */
export function getEffectiveActivityState(agentId: string): string | null {
  const realState = getAgentById(agentId)?.activity_state ?? null;
  if (realState === "THINKING" || realState === "TOOL_RUNNING") {
    return realState;
  }
  if (realState === "IDLE" && getPendingMessages(agentId).some((p) => p.sent_while_idle)) {
    return "THINKING";
  }
  return realState;
}
