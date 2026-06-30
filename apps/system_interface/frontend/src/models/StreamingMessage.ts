/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by agentId so multiple chat panels can subscribe
 * independently; each agent gets its own EventSource.
 */

import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";
import { appendEvents, fetchEvents, type TranscriptEvent } from "./Response";
import { parseJsonMessage } from "./ws-json";
import { openLoginModal } from "./ClaudeAuth";

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

// Last time any frame (event or keepalive) was received per agent. A healthy
// stream delivers either real events or a server keepalive at least every ~8s,
// so a gap past STALE_TIMEOUT_MS means the connection is a half-open/zombie that
// EventSource will never surface as `onerror` -- the staleness watchdog
// force-reconnects it.
const lastActivityByAgent = new Map<string, number>();
const STALE_TIMEOUT_MS = 25_000;
const STALE_CHECK_INTERVAL_MS = 5_000;
let stalenessWatchdog: ReturnType<typeof setInterval> | null = null;

function markActivity(agentId: string): void {
  lastActivityByAgent.set(agentId, Date.now());
}

// DIAG: per-agent SSE counters, read by the e2e via `window.__sseDiag` on a
// freeze to see whether live events are received / buffered / rendered / dropped.
interface SseAgentDiag {
  received: number;
  buffered: number;
  rendered: number;
  dropped: number;
  reconnects: number;
  errors: number;
  inFlightActive: boolean;
  lastId: string | null;
  lastType: string | null;
  lastAt: number;
}
const sseDiagByAgent: Record<string, SseAgentDiag> = {};
(globalThis as unknown as { __sseDiag?: typeof sseDiagByAgent }).__sseDiag = sseDiagByAgent;

function diagFor(agentId: string): SseAgentDiag {
  let diag = sseDiagByAgent[agentId];
  if (diag === undefined) {
    diag = {
      received: 0,
      buffered: 0,
      rendered: 0,
      dropped: 0,
      reconnects: 0,
      errors: 0,
      inFlightActive: false,
      lastId: null,
      lastType: null,
      lastAt: 0,
    };
    sseDiagByAgent[agentId] = diag;
  }
  return diag;
}

// DIAG meta: confirms whether the watchdog timer is actually running (it is
// throttled/suspended in a backgrounded/occluded WebContents) and whether a
// visibility-triggered reconnect fired.
const watchdogMeta = { ticks: 0, visibilityReconnects: 0, lastTickAt: 0 };
(globalThis as unknown as { __sseDiagMeta?: typeof watchdogMeta }).__sseDiagMeta = watchdogMeta;

// Tear down and reconnect (with snapshot refetch) any stream that has gone quiet
// for `thresholdMs` -- a half-open/zombie connection EventSource never surfaces
// as `onerror`. Shared by the interval watchdog and the visibility/focus path.
function reconnectStaleStreams(thresholdMs: number, reason: string): void {
  const now = Date.now();
  for (const [agentId, eventSource] of [...activeStreams]) {
    const last = lastActivityByAgent.get(agentId) ?? now;
    if (now - last <= thresholdMs) {
      continue;
    }
    console.warn(`SSE stream for agent ${agentId} stale (${now - last}ms, ${reason}); forcing reconnect`);
    if (activeStreams.get(agentId) === eventSource) {
      eventSource.close();
      activeStreams.delete(agentId);
    }
    lastActivityByAgent.delete(agentId);
    if (!explicitlyDisconnectedAgents.has(agentId)) {
      void reconnectWithSnapshot(agentId);
    }
  }
}

function ensureStalenessWatchdog(): void {
  if (stalenessWatchdog !== null) {
    return;
  }
  stalenessWatchdog = setInterval(() => {
    watchdogMeta.ticks += 1;
    watchdogMeta.lastTickAt = Date.now();
    reconnectStaleStreams(STALE_TIMEOUT_MS, "watchdog");
  }, STALE_CHECK_INTERVAL_MS);

  // The interval is throttled/suspended while the chat WebContents is hidden or
  // occluded, so a stream that died while backgrounded would never be recovered
  // by the timer alone. When the page becomes visible/focused again, immediately
  // reconnect any stream that has been quiet -- a lower threshold here because
  // the timer may have been asleep.
  const onVisible = (): void => {
    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      return;
    }
    watchdogMeta.visibilityReconnects += 1;
    reconnectStaleStreams(STALE_CHECK_INTERVAL_MS, "visibility");
  };
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisible);
  }
  if (typeof window !== "undefined") {
    window.addEventListener("focus", onVisible);
  }
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
  markActivity(agentId);
  ensureStalenessWatchdog();

  eventSource.onopen = () => {
    // A successful (re)connection resets this agent's backoff.
    getBackoff(agentId).reset();
    markActivity(agentId);
  };

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    // Any frame -- including a keepalive -- means the connection is alive.
    markActivity(agentId);
    const raw = parseJsonMessage<{ type?: string }>(messageEvent.data);
    if (raw === null) {
      return;
    }
    if (raw.type === "keepalive") {
      return;
    }
    const event = raw as TranscriptEvent;
    const diag = diagFor(agentId);
    diag.received += 1;
    diag.lastId = event.event_id;
    diag.lastType = event.type;
    diag.lastAt = Date.now();
    const pending = inFlightSnapshotBuffersByAgent.get(agentId);
    if (pending !== undefined) {
      pending.push(event);
      diag.buffered += 1;
      diag.inFlightActive = true;
    } else {
      diag.inFlightActive = false;
      if (appendEvents(agentId, [event])) {
        diag.rendered += 1;
      } else {
        diag.dropped += 1;
      }
    }
    openLoginModalIfAuthError(event);
  };

  eventSource.onerror = () => {
    diagFor(agentId).errors += 1;
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
  diagFor(agentId).reconnects += 1;
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
  lastActivityByAgent.delete(agentId);
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
