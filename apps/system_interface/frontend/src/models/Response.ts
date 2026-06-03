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
}

const eventsByAgent: Record<string, TranscriptEvent[]> = {};
const notFoundAgentIds = new Set<string>();
const backfillComplete: Record<string, boolean> = {};
// Per-agent step enrichment, keyed by ticket id. Replaced wholesale on each
// snapshot (GET /events and the `step_enrichment` SSE message), never merged.
const enrichmentByAgent: Record<string, Map<string, StepEnrichment>> = {};

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
