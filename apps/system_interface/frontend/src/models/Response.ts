/**
 * Event store for common transcript events.
 * Replaces the LLM response model with events fetched from session files.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface SubagentMetadata {
  agent_type: string;
  description: string;
  session_id: string;
}

export interface ToolCall {
  tool_call_id: string;
  tool_name: string;
  input_preview: string;
  subagent_metadata?: SubagentMetadata;
}

export interface TranscriptEvent {
  timestamp: string;
  type: "user_message" | "assistant_message" | "tool_result";
  event_id: string;
  source: string;
  message_uuid: string;
  session_id?: string;

  // user_message fields
  role?: string;
  content?: string;

  // assistant_message fields
  model?: string;
  text?: string;
  tool_calls?: ToolCall[];
  stop_reason?: string | null;
  usage?: {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens?: number | null;
    cache_write_tokens?: number | null;
  } | null;

  // assistant_message: true when the text matches a known Claude auth-error pattern
  is_auth_error?: boolean;

  // tool_result fields
  tool_call_id?: string;
  tool_name?: string;
  output?: string;
  is_error?: boolean;
}

// For hook compatibility
export interface ResponseItem {
  id: string;
  model: string;
  prompt: string | null;
  system: string | null;
  response: string;
  conversation_id: string;
  datetime_utc: string;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

interface EventsResponse {
  events: TranscriptEvent[];
}

const eventsByAgent: Record<string, TranscriptEvent[]> = {};
const notFoundAgentIds = new Set<string>();
const backfillComplete: Record<string, boolean> = {};

export function isConversationNotFound(agentId: string): boolean {
  return notFoundAgentIds.has(agentId);
}

export function getEventsForAgent(agentId: string): TranscriptEvent[] {
  return eventsByAgent[agentId] ?? [];
}

export function getFirstEventId(agentId: string): string | null {
  const events = eventsByAgent[agentId];
  if (!events || events.length === 0) {
    return null;
  }
  return events[0].event_id;
}

export function isBackfillComplete(agentId: string): boolean {
  return backfillComplete[agentId] === true;
}

/**
 * Merge late-arriving subagent_metadata from a re-broadcast assistant message
 * onto an already-stored one.
 *
 * A running subagent's parent Agent tool_call is streamed before the subagent's
 * session linkage is known, so it first arrives with no subagent_metadata. The
 * backend re-broadcasts the same assistant_message (same event_id) once linkage
 * lands; without this merge appendEvents would discard the re-broadcast as a
 * duplicate and the plain tool-call block would never upgrade to the rich card.
 *
 * Mutates `prior.tool_calls` in place (matched by tool_call_id) and returns
 * whether anything changed.
 */
function mergeLateSubagentMetadata(prior: TranscriptEvent, incoming: TranscriptEvent): boolean {
  if (prior.type !== "assistant_message" || incoming.type !== "assistant_message") {
    return false;
  }
  const incomingByCallId = new Map<string, ToolCall>();
  for (const tc of incoming.tool_calls ?? []) {
    incomingByCallId.set(tc.tool_call_id, tc);
  }
  let changed = false;
  for (const tc of prior.tool_calls ?? []) {
    if (tc.subagent_metadata !== undefined) {
      continue;
    }
    const incomingTc = incomingByCallId.get(tc.tool_call_id);
    if (incomingTc?.subagent_metadata !== undefined) {
      tc.subagent_metadata = incomingTc.subagent_metadata;
      changed = true;
    }
  }
  return changed;
}

export function appendEvents(agentId: string, newEvents: TranscriptEvent[]): void {
  const existing = eventsByAgent[agentId] ?? [];
  const existingById = new Map(existing.map((e) => [e.event_id, e]));
  const brandNewEvents: TranscriptEvent[] = [];
  let didMerge = false;
  for (const event of newEvents) {
    const prior = existingById.get(event.event_id);
    if (prior === undefined) {
      brandNewEvents.push(event);
      existingById.set(event.event_id, event);
    } else if (mergeLateSubagentMetadata(prior, event)) {
      didMerge = true;
    }
  }
  if (brandNewEvents.length > 0) {
    eventsByAgent[agentId] = [...existing, ...brandNewEvents];
    m.redraw();
  } else if (didMerge) {
    m.redraw();
  }
}

export function prependEvents(agentId: string, olderEvents: TranscriptEvent[]): void {
  const existing = eventsByAgent[agentId] ?? [];
  const existingIds = new Set(existing.map((e) => e.event_id));
  const deduped = olderEvents.filter((e) => !existingIds.has(e.event_id));
  if (deduped.length > 0) {
    eventsByAgent[agentId] = [...deduped, ...existing];
    m.redraw();
  } else {
    backfillComplete[agentId] = true;
  }
}

export async function fetchEvents(agentId: string): Promise<TranscriptEvent[]> {
  notFoundAgentIds.delete(agentId);

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId },
    });
    eventsByAgent[agentId] = result.events;
    return result.events;
  } catch (error) {
    const requestError = error as { code?: number; message?: string };
    if (requestError.code === 404) {
      notFoundAgentIds.add(agentId);
    }
    throw error;
  }
}

export async function fetchBackfillEvents(agentId: string): Promise<void> {
  if (backfillComplete[agentId]) {
    return;
  }

  const firstEventId = getFirstEventId(agentId);
  if (!firstEventId) {
    backfillComplete[agentId] = true;
    return;
  }

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, before: firstEventId, limit: "50" },
    });
    if (result.events.length === 0) {
      backfillComplete[agentId] = true;
    } else {
      prependEvents(agentId, result.events);
    }
  } catch {
    // Backfill failure is non-fatal
  }
}

export async function sendMessage(agentId: string, message: string): Promise<void> {
  if (!message.trim()) {
    return;
  }

  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/message"),
    params: { agentId },
    body: { message: message.trim() },
  });
}

// Compatibility shims
export class ConversationNotFoundError extends Error {
  constructor(agentId: string) {
    super(`Agent not found: ${agentId}`);
    this.name = "ConversationNotFoundError";
  }
}

export function getResponsesForConversation(_agentId: string): ResponseItem[] {
  return [];
}

export function getAllResponses(): Record<string, ResponseItem[]> {
  return {};
}

export function getLastResponseModel(_agentId: string): string | null {
  return null;
}

export function appendSyntheticResponse(): void {}

export async function insertResponseItem(): Promise<void> {}

export function fetchResponses(agentId: string): Promise<ResponseItem[]> {
  return fetchEvents(agentId).then(() => []);
}
