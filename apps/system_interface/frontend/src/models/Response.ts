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
 * Fields shared by every event, regardless of `type`. The merged `/events`
 * stream interleaves two independent sources -- the session transcript
 * (user/assistant/tool_result) and the tickets watcher (task_event) -- so
 * the only fields guaranteed on all of them are these transport-level ones.
 */
export interface BaseTranscriptEvent {
  timestamp: string;
  event_id: string;
  source: string;
  // Optional on the base because the two sources disagree: session events
  // (user/assistant/tool_result) always carry message_uuid, but task_events
  // never do. session_id is set only when the backend knows which session
  // file an event came from, so it is conditional on every variant.
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
 * A tk ticket state transition, emitted by the tickets_watcher (one event
 * per (ticket_id, status) tuple, three at most per ticket lifetime). Unlike
 * the other variants this is not a harness-level transcript event: it is
 * parsed from the agent's `.tickets/*.md` files and merged into the same
 * timestamp-ordered stream so the progress view can interleave ticket
 * windows with the transcript.
 */
export interface TaskEvent extends BaseTranscriptEvent {
  // Every field is unconditionally set by the tickets_watcher's
  // `_make_event`, mirroring the TicketState parsed from the `.tickets`
  // file. summary / summary_at are present-but-nullable (null unless the
  // ticket is closed); created_at / parent_id / assignee are always
  // strings but may be empty.
  type: "task_event";
  ticket_id: string;
  title: string;
  status: TaskEventStatus;
  created_at: string;
  summary: string | null;
  summary_at: string | null;
  // True iff the ticket is a turn-bound progress record ("step"), as
  // opposed to a regular tk ticket. Step records nest under their
  // parent ticket in the progress view; standalone steps render flat.
  step: boolean;
  // The id of the ticket this one is nested under, or "" when none.
  parent_id: string;
  // The agent currently assigned to the ticket -- the load-bearing
  // "this is now my work" signal for regular tickets (used by
  // turn-grouping to attribute a picked-up ticket to the picker's
  // first turn rather than the originator's creation turn). "" when
  // unassigned.
  assignee: string;
}

/**
 * A single entry in the merged event stream, discriminated by `type`.
 * Narrow on `event.type` before touching variant-specific fields.
 */
export type TranscriptEvent = UserMessageEvent | AssistantMessageEvent | ToolResultEvent | TaskEvent;

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

export function appendEvents(agentId: string, newEvents: TranscriptEvent[]): void {
  const existing = eventsByAgent[agentId] ?? [];
  const existingIds = new Set(existing.map((e) => e.event_id));
  const deduped = newEvents.filter((e) => !existingIds.has(e.event_id));
  if (deduped.length > 0) {
    eventsByAgent[agentId] = [...existing, ...deduped];
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
