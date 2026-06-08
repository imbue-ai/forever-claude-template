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

export interface PendingMessage {
  /** Stable id for keying the rendered bubble. */
  id: string;
  /** Trimmed message text, matched against transcript user_message content. */
  content: string;
  /** True when the agent was IDLE at send time, so this message should force a
   *  "Thinking..." indicator until it reconciles. False for a message sent to an
   *  already-working agent (its real activity is shown unchanged). */
  sent_while_idle: boolean;
  /** event_ids of user_message events already in the transcript when this
   *  message was sent. Reconciliation ignores these so an older, identical
   *  message can never spuriously "claim" (and hide) this pending one. */
  prior_user_event_ids: Set<string>;
}

let _next_id = 0;

const _pending_by_agent: Record<string, PendingMessage[]> = {};

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
 */
export function addPendingMessage(agentId: string, content: string, currentEvents: readonly TranscriptEvent[]): void {
  const trimmed = content.trim();
  if (!trimmed) {
    return;
  }
  // "force ... if (and ONLY IF) it's totally idle": a working agent already
  // surfaces its own activity, and a null state means activity isn't tracked
  // for this agent at all, so neither should be overridden.
  const sentWhileIdle = getAgentById(agentId)?.activity_state === "IDLE";
  const list = _pending_by_agent[agentId] ?? [];
  list.push({
    id: `pending-${_next_id++}`,
    content: trimmed,
    sent_while_idle: sentWhileIdle,
    prior_user_event_ids: userEventIds(currentEvents),
  });
  _pending_by_agent[agentId] = list;
  m.redraw();
}

/** The still-unreconciled optimistic messages for an agent, in send order. */
export function getPendingMessages(agentId: string): PendingMessage[] {
  return _pending_by_agent[agentId] ?? [];
}

/**
 * Drop optimistic messages whose real transcript event has now arrived.
 *
 * Each pending message is matched to the earliest user_message event that (a)
 * was not already present when the message was sent and (b) has not been
 * claimed by an earlier pending message, so two identical sends reconcile
 * against two distinct transcript events rather than collapsing into one.
 *
 * Matching is by trimmed content: the POST that sends the message is
 * fire-and-forget (no server-assigned id to correlate on), and the backend
 * persists the user's text verbatim into the transcript, so equality holds
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
  const list = _pending_by_agent[agentId];
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
    _pending_by_agent[agentId] = remaining;
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
