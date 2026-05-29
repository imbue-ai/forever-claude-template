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
  // Added by PR 4a: whether older history exists before the first returned
  // event. Absent on responses from an older backend -> treated as false.
  has_more?: boolean;
}

const BACKFILL_PAGE_SIZE = 50;

// Upper bound on events held client-side per agent. Far above any viewport
// window; bounds JS memory for an arbitrarily long conversation while leaving
// generous scrollback resident. Eviction (see evictOldEvents) only trims the
// oldest events and only when the caller is following the live tail.
export const MAX_HELD_EVENTS = 1500;
// Target size to trim down to when evicting, so eviction runs in batches rather
// than on every appended event once at the cap.
export const EVICT_TARGET_EVENTS = 1000;

const eventsByAgent: Record<string, TranscriptEvent[]> = {};
// Persistent per-agent id set mirroring eventsByAgent, so append/prepend dedup
// is O(1) per event instead of rebuilding a Set (O(n)) on every delivery.
const eventIdsByAgent: Record<string, Set<string>> = {};
const notFoundAgentIds = new Set<string>();
// Whether older history exists before the first held event (server has_more, or
// set when we evict local history). Drives scroll-up backfill.
const hasMoreByAgent: Record<string, boolean> = {};

function idSet(agentId: string): Set<string> {
  let set = eventIdsByAgent[agentId];
  if (set === undefined) {
    set = new Set<string>();
    eventIdsByAgent[agentId] = set;
  }
  return set;
}

export function isConversationNotFound(agentId: string): boolean {
  return notFoundAgentIds.has(agentId);
}

export function getEventsForAgent(agentId: string): TranscriptEvent[] {
  return eventsByAgent[agentId] ?? [];
}

export function getEventCount(agentId: string): number {
  return eventsByAgent[agentId]?.length ?? 0;
}

export function getFirstEventId(agentId: string): string | null {
  const events = eventsByAgent[agentId];
  if (!events || events.length === 0) {
    return null;
  }
  return events[0].event_id;
}

export function hasMoreToBackfill(agentId: string): boolean {
  return hasMoreByAgent[agentId] === true;
}

export function isBackfillComplete(agentId: string): boolean {
  return !hasMoreToBackfill(agentId);
}

export function appendEvents(agentId: string, newEvents: TranscriptEvent[]): void {
  const existing = eventsByAgent[agentId] ?? [];
  const ids = idSet(agentId);
  const deduped = newEvents.filter((e) => !ids.has(e.event_id));
  if (deduped.length > 0) {
    for (const event of deduped) {
      ids.add(event.event_id);
    }
    eventsByAgent[agentId] = [...existing, ...deduped];
    m.redraw();
  }
}

export function prependEvents(agentId: string, olderEvents: TranscriptEvent[]): void {
  const existing = eventsByAgent[agentId] ?? [];
  const ids = idSet(agentId);
  const deduped = olderEvents.filter((e) => !ids.has(e.event_id));
  if (deduped.length > 0) {
    for (const event of deduped) {
      ids.add(event.event_id);
    }
    eventsByAgent[agentId] = [...deduped, ...existing];
    m.redraw();
  }
}

/**
 * Drop the oldest events beyond EVICT_TARGET_EVENTS to bound client memory.
 *
 * Returns the number of events removed (0 if under the cap). Callers should
 * only evict while the user is following the live tail, because removing
 * already-rendered older rows would shift a scrolled-up viewport. Since the
 * dropped history still exists on the server, `has_more` is forced true so a
 * later scroll-up re-fetches it via backfill.
 */
export function evictOldEvents(agentId: string): number {
  const existing = eventsByAgent[agentId];
  if (existing === undefined || existing.length <= MAX_HELD_EVENTS) {
    return 0;
  }
  const removeCount = existing.length - EVICT_TARGET_EVENTS;
  const removed = existing.slice(0, removeCount);
  const ids = idSet(agentId);
  for (const event of removed) {
    ids.delete(event.event_id);
  }
  eventsByAgent[agentId] = existing.slice(removeCount);
  hasMoreByAgent[agentId] = true;
  return removeCount;
}

function resetEvents(agentId: string, events: TranscriptEvent[], hasMore: boolean): void {
  eventsByAgent[agentId] = events;
  eventIdsByAgent[agentId] = new Set(events.map((e) => e.event_id));
  hasMoreByAgent[agentId] = hasMore;
}

export async function fetchEvents(agentId: string): Promise<TranscriptEvent[]> {
  notFoundAgentIds.delete(agentId);

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId },
    });
    resetEvents(agentId, result.events, result.has_more === true);
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
  if (!hasMoreToBackfill(agentId)) {
    return;
  }

  const firstEventId = getFirstEventId(agentId);
  if (!firstEventId) {
    hasMoreByAgent[agentId] = false;
    return;
  }

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, before: firstEventId, limit: String(BACKFILL_PAGE_SIZE) },
    });
    if (result.events.length > 0) {
      prependEvents(agentId, result.events);
    }
    // Trust the server's has_more: an empty page or has_more=false ends paging.
    hasMoreByAgent[agentId] = result.has_more === true && result.events.length > 0;
  } catch {
    // Backfill failure is non-fatal; leave has_more set so a later scroll retries.
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
