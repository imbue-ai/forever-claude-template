/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by agentId so multiple chat panels can subscribe
 * independently; each agent gets its own EventSource.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import {
  appendEvents,
  applyEnrichmentSnapshot,
  fetchEvents,
  type StepEnrichment,
  type TranscriptEvent,
} from "./Response";
import { openLoginModal } from "./ClaudeAuth";

const activeStreams = new Map<string, EventSource>();
// Set so an error-triggered reconnect timeout can tell an intentional close
// from a transient error.
const explicitlyDisconnectedAgents = new Set<string>();
// Holds SSE deltas that arrive while a reconnect-time snapshot fetch is in
// flight, so fetchEvents replacing eventsByAgent[agentId] does not drop them.
const inFlightSnapshotBuffersByAgent = new Map<string, TranscriptEvent[]>();

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

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    const raw = JSON.parse(messageEvent.data) as { type?: string };
    // A step_enrichment message is a full enrichment snapshot, not a
    // transcript event -- replace the agent's table and redraw.
    if (raw.type === "step_enrichment") {
      applyEnrichmentSnapshot(agentId, (raw as { enrichment?: Record<string, StepEnrichment> }).enrichment);
      m.redraw();
      return;
    }
    const event = raw as TranscriptEvent;
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
      }, 3000);
    }
  };
}

async function reconnectWithSnapshot(agentId: string): Promise<void> {
  // Subscribe to SSE before the snapshot fetch so deltas that arrive
  // between the snapshot read and the EventSource being registered land in
  // `buffer` instead of being dropped. Hold `buffer` by reference (not via
  // map lookup in `finally`) so a concurrent reconnect that replaces the
  // map slot cannot orphan our buffered events.
  const buffer: TranscriptEvent[] = [];
  inFlightSnapshotBuffersByAgent.set(agentId, buffer);
  connectToStream(agentId);
  try {
    await fetchEvents(agentId);
  } catch (error) {
    console.warn(`Snapshot refetch failed for agent ${agentId} during SSE reconnect`, error);
  } finally {
    if (inFlightSnapshotBuffersByAgent.get(agentId) === buffer) {
      inFlightSnapshotBuffersByAgent.delete(agentId);
    }
    if (buffer.length > 0 && !explicitlyDisconnectedAgents.has(agentId)) {
      appendEvents(agentId, buffer);
    }
  }
}

export function disconnectFromStream(agentId: string): void {
  // Always record the intent, even with no active stream, so a pending
  // error-triggered reconnect timeout sees the tombstone and stays down.
  explicitlyDisconnectedAgents.add(agentId);
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
