/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by agentId so multiple chat panels can subscribe
 * independently; each agent gets its own EventSource.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";
import { appendEvents, fetchEvents, type TranscriptEvent } from "./Response";
import { parseJsonMessage } from "./ws-json";
import { openLoginModal } from "./ClaudeAuth";
import { isNonBoundaryUserMessage } from "../views/user-message-classification";

const activeStreams = new Map<string, EventSource>();
// Set so an error-triggered reconnect timeout can tell an intentional close
// from a transient error.
const explicitlyDisconnectedAgents = new Set<string>();
// Per-agent reconnect backoff, so a healthy stream's success does not reset an
// unhealthy stream's growing delay.
const backoffByAgent = new Map<string, ReconnectBackoff>();

function getBackoff(agentId: string): ReconnectBackoff {
  let backoff = backoffByAgent.get(agentId);
  if (backoff === undefined) {
    backoff = new ReconnectBackoff();
    backoffByAgent.set(agentId, backoff);
  }
  return backoff;
}
// Holds SSE deltas that arrive while a snapshot fetch is in flight (on either
// the initial mount or a reconnect), so fetchEvents replacing
// eventsByAgent[agentId] does not drop them.
const inFlightSnapshotBuffersByAgent = new Map<string, TranscriptEvent[]>();

// Latest in-progress assistant text per agent, from `assistant_streaming` SSE
// frames (mngr's tmux-pane preview of the response being typed). Purely
// transient: never stored in the transcript window, cleared the moment the
// finalized assistant_message lands. An agent with no entry (or an empty one)
// has nothing streaming. See AgentStreamWatcher on the backend.
const streamingPreviewByAgent = new Map<string, string>();

/** The current in-progress assistant text for an agent, or null if nothing is
 *  streaming. The chat view renders this as a provisional bubble at the tail. */
export function getStreamingPreview(agentId: string): string | null {
  const text = streamingPreviewByAgent.get(agentId);
  return text !== undefined && text !== "" ? text : null;
}

function isSpace(ch: string): boolean {
  return /\s/.test(ch);
}

/**
 * Index in ``candidate`` where content not already present (in order) in
 * ``seen`` begins, walking the two together and treating any whitespace run in
 * one as matching a whitespace run (or nothing) in the other.
 *
 * This is the whitespace-tolerant matcher from mngr's reference consumer
 * (``mngr_robinhood``'s ``stream_buffer._unemitted_suffix_start``): mngr's
 * stream buffer is an approximate reverse-map of the tmux pane, so its rendering
 * of a message differs cosmetically (a trailing space, a collapsed blank line
 * around a rule) from the canonical transcript text. Matching whitespace
 * loosely lets us recognize an already-finalized message even when its preview
 * rendering isn't byte-identical -- the bug class an exact compare would miss.
 */
function firstNovelIndex(seen: string, candidate: string): number {
  let seenIndex = 0;
  let candidateIndex = 0;
  let matchedCandidateIndex = 0;
  while (seenIndex < seen.length && candidateIndex < candidate.length) {
    const seenIsSpace = isSpace(seen[seenIndex]);
    const candidateIsSpace = isSpace(candidate[candidateIndex]);
    if (seenIsSpace && candidateIsSpace) {
      while (seenIndex < seen.length && isSpace(seen[seenIndex])) seenIndex++;
      while (candidateIndex < candidate.length && isSpace(candidate[candidateIndex])) candidateIndex++;
      matchedCandidateIndex = candidateIndex;
    } else if (seenIsSpace) {
      seenIndex++;
    } else if (candidateIsSpace) {
      candidateIndex++;
      matchedCandidateIndex = candidateIndex;
    } else if (seen[seenIndex] === candidate[candidateIndex]) {
      seenIndex++;
      candidateIndex++;
      matchedCandidateIndex = candidateIndex;
    } else {
      break;
    }
  }
  return matchedCandidateIndex;
}

/** Whether ``previewText`` carries non-whitespace content beyond what
 *  ``finalizedText`` already covers -- i.e. it is a genuinely in-progress message
 *  rather than the last finalized message lingering in mngr's buffer. */
export function previewHasNewContent(previewText: string, finalizedText: string): boolean {
  return previewText.slice(firstNovelIndex(finalizedText, previewText)).trim() !== "";
}

/**
 * Decide whether the in-progress preview bubble should render.
 *
 * mngr's stream buffer is an approximate, raw view of the agent's tmux pane: it
 * keeps showing the last assistant block until the agent idles, and re-shows it
 * at the start of the next turn before new output streams. mngr hands the
 * consumer the last-complete-id anchor and leaves reconciliation to it (its
 * reference consumer, mngr_robinhood, does the same diffing). So we suppress the
 * bubble when it isn't genuinely in-progress:
 *  - no preview text, or the window is scrolled off the live tail;
 *  - the agent is IDLE -- a settled agent has no response in flight, so nothing
 *    streaming can be current (the hard idle guarantee);
 *  - the preview adds nothing beyond the latest finalized assistant message --
 *    that is the lingering / re-shown last message, compared whitespace-tolerantly
 *    so mngr's cosmetic rendering differences don't defeat the check.
 */
export function shouldShowStreamingPreview(args: {
  previewText: string | null;
  latestAssistantText: string | null;
  activityState: string | null | undefined;
  hasMoreAfter: boolean;
}): boolean {
  const { previewText, latestAssistantText, activityState, hasMoreAfter } = args;
  if (previewText === null || previewText === "") {
    return false;
  }
  if (hasMoreAfter) {
    return false;
  }
  if (activityState === "IDLE") {
    return false;
  }
  if (latestAssistantText !== null && !previewHasNewContent(previewText, latestAssistantText)) {
    return false;
  }
  return true;
}

/** Set (or clear, when empty) an agent's streaming preview, redrawing only when
 *  it actually changed so idle no-op frames cost nothing. */
function setStreamingPreview(agentId: string, text: string): void {
  if ((streamingPreviewByAgent.get(agentId) ?? "") === text) {
    return;
  }
  if (text === "") {
    streamingPreviewByAgent.delete(agentId);
  } else {
    streamingPreviewByAgent.set(agentId, text);
  }
  m.redraw();
}

// Claude auth is mind-global, so an auth-error on any agent's stream
// opens the single shared login modal (see models/ClaudeAuth.ts) -- no
// per-agent routing needed.
function openLoginModalIfAuthError(event: TranscriptEvent): void {
  if (event.type === "assistant_message" && event.is_auth_error === true) {
    openLoginModal();
  }
}

export interface StreamingMessage {
  conversationId: string;
  userPrompt: string;
  model: string | null;
  assistantContent: string;
  finalized: boolean;
  error: string | null;
}

export function connectToStream(agentId: string): void {
  if (activeStreams.has(agentId)) {
    return;
  }

  // A fresh connect supersedes any prior explicit-disconnect tombstone.
  explicitlyDisconnectedAgents.delete(agentId);

  const eventSource = new EventSource(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/stream`));
  activeStreams.set(agentId, eventSource);

  eventSource.onopen = () => {
    // A successful (re)connection resets this agent's backoff.
    getBackoff(agentId).reset();
  };

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    const raw = parseJsonMessage<{ type?: string }>(messageEvent.data);
    if (raw === null) {
      return;
    }
    // An assistant_streaming message is the live, in-progress response preview
    // (not a transcript event); update the provisional bubble and stop.
    if (raw.type === "assistant_streaming") {
      setStreamingPreview(agentId, (raw as { text?: string }).text ?? "");
      return;
    }
    const event = raw as TranscriptEvent;
    // Reset the live preview at turn boundaries so prior-turn text can never
    // linger: a finalized assistant_message supersedes the preview (the durable
    // bubble takes over), and a genuine boundary user_message starts a fresh turn
    // (any body still in mngr's buffer belongs to the turn the user just ended).
    // Fresh frames re-populate the preview once the new response actually streams.
    // Non-boundary user_messages (skill expansions, stop-hook feedback, /welcome)
    // arrive mid-turn while the agent is still streaming, so they must NOT clear
    // the preview -- doing so would flicker the live bubble off until the next
    // snapshot frame. This mirrors how the turn-grouping layer decides boundaries.
    if (event.type === "assistant_message") {
      setStreamingPreview(agentId, "");
    } else if (event.type === "user_message" && !isNonBoundaryUserMessage(event.content)) {
      setStreamingPreview(agentId, "");
    }
    const pending = inFlightSnapshotBuffersByAgent.get(agentId);
    if (pending !== undefined) {
      pending.push(event);
    } else {
      appendEvents(agentId, [event]);
    }
    openLoginModalIfAuthError(event);
  };

  eventSource.onerror = () => {
    if (activeStreams.get(agentId) === eventSource) {
      eventSource.close();
      activeStreams.delete(agentId);
      setTimeout(() => {
        const wasExplicitlyDisconnected = explicitlyDisconnectedAgents.delete(agentId);
        if (!wasExplicitlyDisconnected && !activeStreams.has(agentId)) {
          void reconnectWithSnapshot(agentId);
        }
      }, getBackoff(agentId).nextDelay());
    }
  };
}

/**
 * Open the live SSE stream and fetch the snapshot together, buffering any SSE
 * deltas that arrive while the snapshot fetch is in flight.
 *
 * `fetchEvents` replaces `eventsByAgent[agentId]` wholesale with the snapshot,
 * so a delta that arrives between the stream opening and the snapshot landing
 * would otherwise be overwritten and lost. Both the initial mount and the
 * reconnect path go through here so neither can drop events. Re-throws fetch
 * errors so the caller can surface a load error; buffered deltas are flushed
 * first regardless.
 */
export async function loadSnapshotWithStream(agentId: string): Promise<void> {
  // Subscribe to SSE before the snapshot fetch so deltas that arrive
  // between the snapshot read and the EventSource being registered land in
  // `buffer` instead of being dropped. Hold `buffer` by reference (not via
  // map lookup in `finally`) so a concurrent load that replaces the
  // map slot cannot orphan our buffered events.
  const buffer: TranscriptEvent[] = [];
  inFlightSnapshotBuffersByAgent.set(agentId, buffer);
  connectToStream(agentId);
  try {
    await fetchEvents(agentId);
  } finally {
    if (inFlightSnapshotBuffersByAgent.get(agentId) === buffer) {
      inFlightSnapshotBuffersByAgent.delete(agentId);
    }
    if (buffer.length > 0 && !explicitlyDisconnectedAgents.has(agentId)) {
      appendEvents(agentId, buffer);
    }
  }
}

async function reconnectWithSnapshot(agentId: string): Promise<void> {
  try {
    await loadSnapshotWithStream(agentId);
  } catch (error) {
    console.warn(`Snapshot refetch failed for agent ${agentId} during SSE reconnect`, error);
  }
}

export function disconnectFromStream(agentId: string): void {
  // Always record the intent, even with no active stream, so a pending
  // error-triggered reconnect timeout sees the tombstone and stays down.
  explicitlyDisconnectedAgents.add(agentId);
  // Drop the backoff so a later fresh connectToStream starts from the base
  // delay rather than inheriting a stale grown delay.
  backoffByAgent.delete(agentId);
  // Drop any in-progress preview so a stale bubble doesn't linger after the
  // stream is intentionally closed (e.g. switching away from this agent).
  streamingPreviewByAgent.delete(agentId);
  const eventSource = activeStreams.get(agentId);
  if (eventSource !== undefined) {
    eventSource.close();
    activeStreams.delete(agentId);
  }
}

// Compatibility shims
export function getStreamingMessage(_agentId: string): StreamingMessage | null {
  return null;
}

export function isStreaming(): boolean {
  return false;
}

export function clearStreamingMessage(): void {}

export function consumeLastFinalizedMessage(): StreamingMessage | null {
  return null;
}

export function startStreamingMessage(): void {}
export function appendStreamingDelta(): void {}
export function finalizeStreamingMessage(): void {}
export function markStreamingError(): void {}
