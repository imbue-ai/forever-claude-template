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
  // For Agent tool calls: the description and subagent_type from the tool input, present
  // as soon as the call appears so the rich card can render before the subagent session is
  // linked. subagent_metadata (with the session_id for the click-through) is filled in once
  // the linkage is resolved.
  description?: string;
  subagent_type?: string;
  subagent_metadata?: SubagentMetadata;
}

/**
 * Status vocabulary mirrored from the tk ticket tracker:
 *   - "open"        -> rendered as "pending" in the chat progress UI
 *   - "in_progress" -> rendered as "active"
 *   - "closed"      -> rendered as "done"
 * There is no failed state by design (every ticket terminates as closed
 * with a summary; see CLAUDE.md "Task management" in the FCT side).
 */
export type TaskEventStatus = "open" | "in_progress" | "closed";

/**
 * Fields shared by every event, regardless of `type`. The `/events` stream is
 * the session transcript (user/assistant/tool_result); these are the only
 * transport-level fields guaranteed on all variants. (tk step state is not in
 * this stream -- it ships as a separate enrichment snapshot, see
 * StepEnrichment.)
 */
export interface BaseTranscriptEvent {
  timestamp: string;
  event_id: string;
  source: string;
  // message_uuid is always set for transcript events; session_id is set only
  // when the backend knows which session file an event came from, so it is
  // conditional on every variant.
  message_uuid?: string;
  session_id?: string;
}

/**
 * A message from the user (or a hook/system message rendered as one).
 * session_parser only emits this event when there is real user text, so
 * `content` is always present and non-empty.
 */
export interface UserMessageEvent extends BaseTranscriptEvent {
  type: "user_message";
  role: string;
  content: string;
}

/**
 * A model turn: prose text and/or tool calls. Every field below is always
 * present in the backend's emit (`session_parser._parse_assistant_message`);
 * `text` may be empty and `tool_calls` may be empty, but the keys are always
 * there, and `stop_reason` / `usage` are present-but-nullable.
 */
export interface AssistantMessageEvent extends BaseTranscriptEvent {
  type: "assistant_message";
  model: string;
  text: string;
  tool_calls: ToolCall[];
  stop_reason: string | null;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number | null;
    cache_write_tokens: number | null;
  } | null;
  // True when the text matches a known Claude auth-error pattern.
  is_auth_error: boolean;
}

/**
 * The result of a single tool call, keyed back by `tool_call_id`.
 * session_parser skips emitting a tool_result with no tool_use_id, so when
 * one exists `tool_call_id` is always a non-empty string.
 */
export interface ToolResultEvent extends BaseTranscriptEvent {
  type: "tool_result";
  tool_call_id: string;
  tool_name: string;
  output: string;
  is_error: boolean;
}

/**
 * Per-step enrichment, keyed by ticket id, delivered as a snapshot alongside
 * the transcript (the `step_enrichment` field on the events response and the
 * `step_enrichment` SSE message). tk owns this side-table: canonical title,
 * close summary, current status, and the creation timestamp (used only to
 * order not-yet-started steps). The progress view derives all structure from
 * the transcript and joins this in by id; it never determines order or
 * grouping.
 */
export interface StepEnrichment {
  title: string;
  summary: string | null;
  status: TaskEventStatus;
  created_at: string;
}

/**
 * A single entry in the transcript event stream, discriminated by `type`.
 * Narrow on `event.type` before touching variant-specific fields.
 */
export type TranscriptEvent = UserMessageEvent | AssistantMessageEvent | ToolResultEvent;

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
  // Full, unpaginated snapshot of the agent's step enrichment keyed by ticket
  // id. Always complete regardless of where the transcript window is, so a
  // freshly-loaded tail still has titles/summaries for every visible step.
  step_enrichment?: Record<string, StepEnrichment>;
  // Whether older history exists before the first returned event, so the client
  // can page back on scroll without a probe request. Absent on an older backend
  // response -> treated as false.
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
// Persistent per-agent index from event_id to the stored event object,
// mirroring eventsByAgent. Gives O(1) dedup on append/prepend (instead of
// rebuilding a Set on every SSE delivery) and O(1) lookup of an already-stored
// event so a re-broadcast can upgrade it in place (see appendEvents).
const eventByIdByAgent: Record<string, Map<string, TranscriptEvent>> = {};
const notFoundAgentIds = new Set<string>();
// Whether older history exists before the first held event (server has_more, or
// set when we evict local history). Drives scroll-up backfill.
const hasMoreByAgent: Record<string, boolean> = {};
// Per-agent step enrichment, keyed by ticket id. Replaced wholesale on each
// snapshot (GET /events and the `step_enrichment` SSE message), never merged.
const enrichmentByAgent: Record<string, Map<string, StepEnrichment>> = {};

function idMap(agentId: string): Map<string, TranscriptEvent> {
  let map = eventByIdByAgent[agentId];
  if (map === undefined) {
    map = new Map<string, TranscriptEvent>();
    eventByIdByAgent[agentId] = map;
  }
  return map;
}

export function getEnrichmentForAgent(agentId: string): Map<string, StepEnrichment> {
  return enrichmentByAgent[agentId] ?? new Map();
}

/** Replace an agent's enrichment table from a snapshot. Does not redraw --
 *  callers in a fetch/redraw flow already trigger one; the SSE path redraws
 *  explicitly. */
export function applyEnrichmentSnapshot(agentId: string, snapshot: Record<string, StepEnrichment> | undefined): void {
  enrichmentByAgent[agentId] = new Map(Object.entries(snapshot ?? {}));
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
  // The persistent index gives O(1) dedup, and -- because it stores the event
  // object, not just its id -- O(1) lookup of an already-held event so a
  // re-broadcast (same event_id) can upgrade it in place rather than being
  // dropped as a duplicate.
  const byId = idMap(agentId);
  const brandNewEvents: TranscriptEvent[] = [];
  let didMerge = false;
  for (const event of newEvents) {
    const prior = byId.get(event.event_id);
    if (prior === undefined) {
      brandNewEvents.push(event);
      byId.set(event.event_id, event);
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
  const byId = idMap(agentId);
  const deduped = olderEvents.filter((e) => !byId.has(e.event_id));
  if (deduped.length > 0) {
    for (const event of deduped) {
      byId.set(event.event_id, event);
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
  const byId = idMap(agentId);
  for (const event of removed) {
    byId.delete(event.event_id);
  }
  eventsByAgent[agentId] = existing.slice(removeCount);
  hasMoreByAgent[agentId] = true;
  return removeCount;
}

function resetEvents(agentId: string, events: TranscriptEvent[], hasMore: boolean): void {
  eventsByAgent[agentId] = events;
  eventByIdByAgent[agentId] = new Map(events.map((e) => [e.event_id, e]));
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
    applyEnrichmentSnapshot(agentId, result.step_enrichment);
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
  } catch (error) {
    // Backfill failure is non-fatal: the older history just isn't loaded, and
    // has_more stays set so the next scroll retries. Log it so a persistent
    // failure is diagnosable instead of vanishing silently.
    console.warn(`Failed to backfill older events for agent ${agentId}`, error);
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

export async function interruptAgent(agentId: string): Promise<void> {
  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/interrupt"),
    params: { agentId },
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
