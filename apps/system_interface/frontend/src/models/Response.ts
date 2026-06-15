/**
 * Event store for common transcript events.
 * Replaces the LLM response model with events fetched from session files.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { reconcilePendingMessages } from "./PendingMessages";

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
 * Fields shared by every event, regardless of `type`. The `/events` stream is
 * the session transcript (user/assistant/tool_result); these are the only
 * transport-level fields guaranteed on all variants. tk step state (titles,
 * summaries) is carried in the transcript itself -- the lines tk prints on
 * stdout -- not in any side-channel.
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
  // Global index of the first returned event within the full transcript, and the
  // transcript's total length. Together they place the loaded window in the whole
  // conversation: the client sizes the scrollbar for `total` and derives whether
  // more history exists above (offset > 0) and below (offset + events < total).
  offset?: number;
  total?: number;
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

// All per-agent transcript state is owned by one TranscriptStore instance per
// agent (see storeByAgent below). The held events are a single contiguous window
// of the full transcript: `firstOffset` is the global index of events[0] and
// `total` the full length; whether more history exists above/below and the
// scrollbar size are derived from those two. The window can sit anywhere (the live
// tail is just the case where it ends at `total`), so it pages in both directions
// and can be replaced wholesale by a jump to an arbitrary offset.
//
// `renderVersion` is a monotonic counter the chat view memoizes its (expensive)
// turn-grouping on, so a scroll-only redraw -- which changes no data -- reuses the
// cached rows instead of re-walking the whole held transcript every frame (the
// dominant scroll cost on a long conversation). Its invariant: it must bump on
// every mutation that changes what renders and never on a no-op. The fields are
// #private and the only writer of renderVersion is the private #commit funnel, so
// no mutation -- here or in a future method -- can change the store without going
// through the one place the version is bumped. The store is a pure data holder: a
// mutation returns whether anything changed so the module-level wrappers can decide
// redraws, but the store never touches the view layer itself.
class TranscriptStore {
  #events: TranscriptEvent[] = [];
  // event_id -> stored event, mirroring #events: O(1) dedup on append/prepend and
  // O(1) lookup so a re-broadcast can upgrade an event in place (see append).
  #byId = new Map<string, TranscriptEvent>();
  #firstOffset = 0;
  // Total events in the full server-side transcript (see EventsResponse.total).
  #total = 0;
  #renderVersion = 0;

  get events(): TranscriptEvent[] {
    return this.#events;
  }

  get eventCount(): number {
    return this.#events.length;
  }

  get firstOffset(): number {
    return this.#firstOffset;
  }

  /** Total events in the full transcript, for scrollbar sizing. Never less than
   *  the loaded window's end, so the window always fits inside it. */
  get total(): number {
    return Math.max(this.#total, this.#firstOffset + this.#events.length);
  }

  get renderVersion(): number {
    return this.#renderVersion;
  }

  /** Older history exists before the window (it doesn't start at 0). */
  get hasMoreBefore(): boolean {
    return this.#firstOffset > 0;
  }

  /** Newer history exists after the window (it doesn't reach the live tail) --
   *  true only after a jump/scroll moved the window off the end. */
  get hasMoreAfter(): boolean {
    return this.#firstOffset + this.#events.length < this.total;
  }

  get firstEventId(): string | null {
    return this.#events.length > 0 ? this.#events[0].event_id : null;
  }

  get lastEventId(): string | null {
    return this.#events.length > 0 ? this.#events[this.#events.length - 1].event_id : null;
  }

  /**
   * The single mutation funnel and the ONLY writer of #renderVersion. Each mutator
   * expresses its change as `mutate` and returns whether anything that renders
   * changed; the version bumps iff it did, and #commit returns that flag so the
   * caller can skip a redraw on a no-op. Private alongside the #private fields, this
   * is what makes the bump impossible to forget -- there is no other way to change
   * the store.
   */
  #commit(mutate: () => boolean): boolean {
    const changed = mutate();
    if (changed) {
      this.#renderVersion += 1;
    }
    return changed;
  }

  /**
   * Append live tail events. They only belong in the window when it is
   * tail-anchored (reaches the live end); if the user has jumped to an earlier
   * position, appending would break contiguity, so brand-new events are dropped
   * (re-fetched via forward paging on return to the tail). A late re-broadcast that
   * upgrades an already-held event in place is applied regardless of position.
   */
  append(newEvents: TranscriptEvent[]): boolean {
    const tailAnchored = !this.hasMoreAfter;
    return this.#commit(() => {
      let added = false;
      let merged = false;
      for (const event of newEvents) {
        const prior = this.#byId.get(event.event_id);
        if (prior === undefined) {
          if (tailAnchored) {
            this.#events.push(event);
            this.#byId.set(event.event_id, event);
            added = true;
          }
        } else if (mergeLateSubagentMetadata(prior, event)) {
          merged = true;
        }
      }
      if (added) {
        // Tail-anchored, so the window still reaches the end: total grows with it.
        this.#total = this.#firstOffset + this.#events.length;
      }
      return added || merged;
    });
  }

  /**
   * Prepend an older page. When `offset` is given (the global index of the page's
   * first event, from the server) it becomes the window's new start; otherwise the
   * start shifts back by the number of events added (used by tests that prepend
   * without a server round-trip).
   */
  prepend(olderEvents: TranscriptEvent[], offset?: number, total?: number): boolean {
    return this.#commit(() => {
      const deduped = olderEvents.filter((e) => !this.#byId.has(e.event_id));
      if (deduped.length === 0) {
        return false;
      }
      for (const event of deduped) {
        this.#byId.set(event.event_id, event);
      }
      this.#events = [...deduped, ...this.#events];
      this.#firstOffset = offset !== undefined ? offset : Math.max(0, this.#firstOffset - deduped.length);
      if (total !== undefined) {
        this.#total = total;
      }
      return true;
    });
  }

  /** Append a newer page (paging toward the tail from a window moved off the end
   *  by a jump). The window start is unchanged. */
  appendForward(newerEvents: TranscriptEvent[], total?: number): boolean {
    return this.#commit(() => {
      const deduped = newerEvents.filter((e) => !this.#byId.has(e.event_id));
      if (deduped.length === 0) {
        return false;
      }
      for (const event of deduped) {
        this.#byId.set(event.event_id, event);
      }
      this.#events = [...this.#events, ...deduped];
      if (total !== undefined) {
        this.#total = total;
      }
      return true;
    });
  }

  /**
   * Drop the oldest events beyond EVICT_TARGET_EVENTS to bound client memory,
   * returning the number removed (0 if under the cap). The window start advances by
   * that count, so the dropped history (still on the server) is re-fetched via
   * backfill on a later scroll-up. Callers evict only while following the live tail,
   * since removing already-rendered older rows would shift a scrolled-up viewport.
   */
  evict(): number {
    let removeCount = 0;
    this.#commit(() => {
      if (this.#events.length <= MAX_HELD_EVENTS) {
        return false;
      }
      removeCount = this.#events.length - EVICT_TARGET_EVENTS;
      const removed = this.#events.slice(0, removeCount);
      for (const event of removed) {
        this.#byId.delete(event.event_id);
      }
      this.#events = this.#events.slice(removeCount);
      this.#firstOffset += removeCount;
      return true;
    });
    return removeCount;
  }

  /** Replace the held window wholesale (initial load, or a jump to an offset). */
  reset(events: TranscriptEvent[], offset: number, total: number): void {
    this.#commit(() => {
      this.#events = events;
      this.#byId = new Map(events.map((e) => [e.event_id, e]));
      this.#firstOffset = offset;
      this.#total = total;
      return true;
    });
  }

  /** An older page came back empty: the window already starts at the beginning. */
  markReachedStart(total?: number): void {
    this.#commit(() => {
      this.#firstOffset = 0;
      if (total !== undefined) {
        this.#total = total;
      }
      return true;
    });
  }

  /** A newer page came back empty: the window reaches the live tail; reconcile the
   *  total the server now reports. */
  reconcileTotalAtTail(total: number): void {
    this.#commit(() => {
      this.#total = total;
      return true;
    });
  }
}

const storeByAgent: Record<string, TranscriptStore> = {};
const notFoundAgentIds = new Set<string>();

function storeFor(agentId: string): TranscriptStore {
  let store = storeByAgent[agentId];
  if (store === undefined) {
    store = new TranscriptStore();
    storeByAgent[agentId] = store;
  }
  return store;
}

// Read accessors. These never create a store, so an unknown agent reads as empty
// defaults rather than allocating one on a mere read.
export function getRenderVersion(agentId: string): number {
  return storeByAgent[agentId]?.renderVersion ?? 0;
}

export function getFirstOffset(agentId: string): number {
  return storeByAgent[agentId]?.firstOffset ?? 0;
}

export function getTotalEventCount(agentId: string): number {
  return storeByAgent[agentId]?.total ?? 0;
}

export function hasMoreBefore(agentId: string): boolean {
  return storeByAgent[agentId]?.hasMoreBefore ?? false;
}

export function hasMoreAfter(agentId: string): boolean {
  return storeByAgent[agentId]?.hasMoreAfter ?? false;
}

export function isConversationNotFound(agentId: string): boolean {
  return notFoundAgentIds.has(agentId);
}

export function getEventsForAgent(agentId: string): TranscriptEvent[] {
  return storeByAgent[agentId]?.events ?? [];
}

export function getEventCount(agentId: string): number {
  return storeByAgent[agentId]?.eventCount ?? 0;
}

export function getFirstEventId(agentId: string): string | null {
  return storeByAgent[agentId]?.firstEventId ?? null;
}

export function getLastEventId(agentId: string): string | null {
  return storeByAgent[agentId]?.lastEventId ?? null;
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
  if (storeFor(agentId).append(newEvents)) {
    // A live transcript event may be the real counterpart of an optimistic
    // message the user just sent; drop any such bubble now that it has landed.
    reconcilePendingMessages(agentId, getEventsForAgent(agentId));
    m.redraw();
  }
}

export function prependEvents(agentId: string, olderEvents: TranscriptEvent[], offset?: number, total?: number): void {
  if (storeFor(agentId).prepend(olderEvents, offset, total)) {
    m.redraw();
  }
}

export function appendForwardEvents(agentId: string, newerEvents: TranscriptEvent[], total?: number): void {
  if (storeFor(agentId).appendForward(newerEvents, total)) {
    m.redraw();
  }
}

export function evictOldEvents(agentId: string): number {
  return storeFor(agentId).evict();
}

function placeWindow(agentId: string, result: EventsResponse): void {
  const offset = result.offset ?? 0;
  const total = result.total ?? offset + result.events.length;
  const store = storeFor(agentId);
  store.reset(result.events, offset, total);
}

export async function fetchEvents(agentId: string): Promise<TranscriptEvent[]> {
  notFoundAgentIds.delete(agentId);

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId },
    });
    placeWindow(agentId, result);
    // A snapshot reload (initial load or reconnect) may already contain the
    // real counterpart of an optimistic message; reconcile against it too.
    reconcilePendingMessages(agentId, result.events);
    return result.events;
  } catch (error) {
    const requestError = error as { code?: number; message?: string };
    if (requestError.code === 404) {
      notFoundAgentIds.add(agentId);
    }
    throw error;
  }
}

/** Jump the window to an arbitrary global offset in one request (e.g. a scrollbar
 *  drag far from the loaded window), replacing the held events. */
export async function fetchWindowAtOffset(agentId: string, offset: number): Promise<void> {
  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, offset: String(Math.max(0, offset)), limit: String(BACKFILL_PAGE_SIZE) },
    });
    placeWindow(agentId, result);
  } catch (error) {
    console.warn(`Failed to load events at offset ${offset} for agent ${agentId}`, error);
  }
}

export async function fetchBackfillEvents(agentId: string): Promise<void> {
  if (!hasMoreBefore(agentId)) {
    return;
  }
  const firstEventId = getFirstEventId(agentId);
  if (!firstEventId) {
    return;
  }

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, before: firstEventId, limit: String(BACKFILL_PAGE_SIZE) },
    });
    if (result.events.length > 0) {
      prependEvents(agentId, result.events, result.offset, result.total);
    } else {
      // Nothing before the cursor: the window already starts at the beginning.
      storeFor(agentId).markReachedStart(result.total);
    }
  } catch (error) {
    // Backfill failure is non-fatal: the older history just isn't loaded, and
    // the window start is unchanged so the next scroll retries. Log it so a
    // persistent failure is diagnosable instead of vanishing silently.
    console.warn(`Failed to backfill older events for agent ${agentId}`, error);
  }
}

export async function fetchForwardEvents(agentId: string): Promise<void> {
  if (!hasMoreAfter(agentId)) {
    return;
  }
  const lastEventId = getLastEventId(agentId);
  if (!lastEventId) {
    return;
  }

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, after: lastEventId, limit: String(BACKFILL_PAGE_SIZE) },
    });
    if (result.events.length > 0) {
      appendForwardEvents(agentId, result.events, result.total);
    } else if (result.total !== undefined) {
      // Nothing after the cursor: the window reaches the live tail.
      storeFor(agentId).reconcileTotalAtTail(result.total);
    }
  } catch (error) {
    console.warn(`Failed to load newer events for agent ${agentId}`, error);
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
