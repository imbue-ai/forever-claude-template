/**
 * Step grouping: a single in-order walk of the transcript.
 *
 * The progress view is a frontend for the transcript. Structure -- which
 * steps exist, their order, their open/close transitions, which events
 * belong to which step -- is read from transcript *position*. tk lifecycle
 * commands (`tk create/start/close`) appear in the transcript as Bash tool
 * calls; their results carry the canonical id and status
 * (`Updated <id> -> <status>`), which is all the structure we need.
 *
 * tk also provides an *enrichment* side-table keyed by id: the canonical
 * title, the close summary, and the roster of pending (not-yet-started)
 * steps. Enrichment decorates the transcript-derived skeleton by id; it does
 * not decide order or grouping.
 *
 * The walk maintains a single "current open step": events while a step is
 * open group under it; events while none is open fall into an ungrouped
 * run rendered inline (the same plain-chat path used for turns with no
 * steps). A step still open when the next user message arrives carries over:
 * it re-renders at the top of the new turn, while the prior turn's node
 * freezes at its last-known state.
 *
 * The ONLY timestamp this module reads is tk's own `created` (from the
 * enrichment table), used solely to order pending placeholders among
 * themselves. Grouping and the positioning of any transitioned step read
 * transcript order alone.
 */

import type {
  TranscriptEvent,
  AssistantMessageEvent,
  UserMessageEvent,
  ToolResultEvent,
  ToolCall,
  StepEnrichment,
} from "../models/Response";
import { isNonBoundaryUserMessage, isStopHookFeedback } from "./user-message-classification";

export type StepStatus = "pending" | "active" | "done";

/** A step as it should render in one section. The same ticket id can produce
 *  two independent nodes across two sections (carryover); each holds its own
 *  state and never updates the other. */
export interface StepNode {
  ticket_id: string;
  title: string;
  status: StepStatus;
  /** Close summary, shown when done. */
  summary: string | null;
  /** Latest in-step prose that was followed by more work in the same step. */
  narration: string | null;
  /** True when this node carried over from a prior section (re-rendered at the
   *  top of this section because it was still open at the boundary). */
  is_carryover: boolean;
  /** True when this is the live step the agent is currently on -- the only one
   *  that may show a spinner. False once settled (idle, past section, or
   *  superseded by a later step). */
  is_frontier: boolean;
  /** The grouped real-work events (assistant text + non-tk tool calls) that
   *  occurred while this step was the open step in this section. */
  events: AssistantMessageEvent[];
}

/** One item on a section's timeline, in transcript order. */
export type TimelineItem =
  | { kind: "step"; step: StepNode }
  /** Real work (and/or prose) that happened while no step was open. Rendered
   *  inline, exactly like a no-steps plain-chat turn. */
  | { kind: "ungrouped"; key: string; events: AssistantMessageEvent[] }
  /** A non-boundary user message shown inline (e.g. a stop-hook chip). */
  | { kind: "chip"; event: UserMessageEvent };

/** A turn: the user message, its timeline, and the wrap-up reply below it. */
export interface SectionView {
  /** The boundary user message that opened this section, or null for content
   *  that precedes the first user message. */
  user_event: UserMessageEvent | null;
  key: string;
  items: TimelineItem[];
  /** Text after the last real (non-tk) tool activity: the user-facing reply,
   *  rendered below the timeline. */
  trailing_reply: AssistantMessageEvent[];
}

/** Detects a `tk`/`ticket` lifecycle invocation in a Bash tool call's input
 *  preview. The verb sits at the front of the command, so this survives the
 *  200-char input_preview truncation. `super` is the plugin-bypassing form. */
const TK_LIFECYCLE_RE = /\b(?:tk|ticket)\s+(?:super\s+)?(?:create|start|close)\b/;

/** A status transition line printed by tk on every state change:
 *  `Updated <id> -> <status>` (see vendor/tk/ticket). Global so a batched
 *  command that flips several tickets is read in order. */
const TK_UPDATED_RE = /Updated\s+(\S+)\s+->\s+(open|in_progress|closed)/g;

/** True when a tool call is a tk lifecycle command (consumed as a structural
 *  marker, not rendered as work). */
function isTkLifecycleCall(tc: ToolCall): boolean {
  return TK_LIFECYCLE_RE.test(tc.input_preview);
}

interface ParsedMessage {
  /** Start/close transitions this message caused, in order. (Creates are not
   *  positioned here -- pending steps come from the enrichment roster.) */
  transitions: { id: string; status: "in_progress" | "closed" }[];
  /** The renderable remainder: the message stripped of its tk lifecycle calls.
   *  Null when nothing renderable remains (a pure tk command). */
  render: AssistantMessageEvent | null;
}

/** Split an assistant message into the tk transitions it caused and the
 *  renderable remainder (text + non-tk tool calls). */
function parseMessage(e: AssistantMessageEvent, toolResults: Map<string, ToolResultEvent>): ParsedMessage {
  const tkCalls = e.tool_calls.filter(isTkLifecycleCall);
  const realCalls = e.tool_calls.filter((tc) => !isTkLifecycleCall(tc));

  const transitions: { id: string; status: "in_progress" | "closed" }[] = [];
  for (const tc of tkCalls) {
    const output = toolResults.get(tc.tool_call_id)?.output ?? "";
    TK_UPDATED_RE.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = TK_UPDATED_RE.exec(output)) !== null) {
      const status = match[2];
      if (status === "in_progress" || status === "closed") {
        transitions.push({ id: match[1], status });
      }
    }
  }

  if (tkCalls.length === 0) {
    return { transitions, render: e };
  }
  // Pure tk command (no text, no real work): fully consumed.
  if (!e.text && realCalls.length === 0) {
    return { transitions, render: null };
  }
  return { transitions, render: { ...e, tool_calls: realCalls } };
}

/** True when a renderable message represents real work (issues a non-tk tool
 *  call), as opposed to prose. */
function isWork(e: AssistantMessageEvent): boolean {
  return e.tool_calls.length > 0;
}

/** True when a renderable message is prose (has text, no tool calls). */
function isProse(e: AssistantMessageEvent): boolean {
  return !!e.text && e.tool_calls.length === 0;
}

// --- Section assembly ---

/** A routed message plus where it landed: under a step (by id) or ungrouped. */
interface Placement {
  event: AssistantMessageEvent;
  step_id: string | null;
}

/** An ordered skeleton entry recorded as the transcript is walked. A `step`
 *  entry marks where a step node first appears -- its first transition (an
 *  open, or a close with no prior open) -- so the node is positioned by that
 *  transition even when the step carries no work. An `event` entry is a routed
 *  assistant message. Timeline items are rebuilt from these in transcript
 *  order. */
type SectionEntry =
  | { kind: "step"; id: string }
  | { kind: "event"; event: AssistantMessageEvent; step_id: string | null };

interface SectionBuilder {
  user_event: UserMessageEvent | null;
  key: string;
  /** Step nodes in first-appearance (transcript) order. */
  steps: Map<string, StepNode>;
  step_order: string[];
  placements: Placement[];
  /** Ordered skeleton of step appearances and routed events (see SectionEntry),
   *  used to assemble timeline items in transcript order. */
  entries: SectionEntry[];
  /** Non-boundary user-message chips, with the index into `entries` they
   *  follow, so they render at their chronological spot. */
  chips: { event: UserMessageEvent; after: number }[];
  current_step_id: string | null;
}

function newSection(user_event: UserMessageEvent | null, key: string): SectionBuilder {
  return {
    user_event,
    key,
    steps: new Map(),
    step_order: [],
    placements: [],
    entries: [],
    chips: [],
    current_step_id: null,
  };
}

/** Walk the visible transcript into ordered sections. `toolResults` resolves
 *  tk command outputs (and is reused by the renderer). `enrichment` supplies
 *  titles, summaries, and the pending roster. `agentIsIdle` settles the
 *  spinner on the tail section. */
export function buildSections(
  events: TranscriptEvent[],
  toolResults: Map<string, ToolResultEvent>,
  enrichment: Map<string, StepEnrichment>,
  agentIsIdle: boolean,
): SectionView[] {
  const builders: SectionBuilder[] = [];
  let current: SectionBuilder | null = null;
  // Steps open at the end of the prior section, to re-open as carryover.
  let carryover: string[] = [];

  const ensureSection = (user_event: UserMessageEvent | null, key: string): SectionBuilder => {
    const section = newSection(user_event, key);
    // Re-open carried-over steps at the top of the new section.
    for (const id of carryover) {
      openStep(section, id, /* is_carryover */ true);
    }
    carryover = [];
    builders.push(section);
    return section;
  };

  for (const e of events) {
    if (e.type === "user_message") {
      if (isNonBoundaryUserMessage(e.content ?? "")) {
        // Stop-hook feedback and the like: a chip inside the current section.
        if (current !== null && isStopHookFeedback(e.content ?? "")) {
          current.chips.push({ event: e, after: current.entries.length - 1 });
        }
        // Hidden non-boundary messages (skill expansions, /welcome) are dropped.
        continue;
      }
      // Real user turn: close the prior section (carrying open steps) and open
      // a new one.
      carryover = current === null ? [] : openStepsAtEnd(current);
      current = ensureSection(e, `section-${e.event_id}`);
      continue;
    }
    if (e.type === "assistant_message") {
      if (current === null) current = ensureSection(null, "section-pre");
      const parsed = parseMessage(e, toolResults);
      for (const t of parsed.transitions) applyTransition(current, t);
      if (parsed.render !== null && (parsed.render.text || parsed.render.tool_calls.length > 0)) {
        routeMessage(current, parsed.render);
      }
      continue;
    }
    // tool_result events are resolved by id via toolResults; no routing needed.
  }

  return builders.map((b) => finalizeSection(b, enrichment, agentIsIdle, b === builders[builders.length - 1]));
}

/** Open (or re-open) a step node as the current step. */
function openStep(section: SectionBuilder, id: string, is_carryover: boolean): void {
  if (!section.steps.has(id)) {
    section.steps.set(id, {
      ticket_id: id,
      title: id,
      status: "active",
      summary: null,
      narration: null,
      is_carryover,
      is_frontier: false,
      events: [],
    });
    section.step_order.push(id);
    section.entries.push({ kind: "step", id });
  }
  section.current_step_id = id;
}

function applyTransition(section: SectionBuilder, t: { id: string; status: "in_progress" | "closed" }): void {
  if (t.status === "in_progress") {
    openStep(section, t.id, /* is_carryover */ false);
    return;
  }
  // closed
  const node = section.steps.get(t.id);
  if (node !== undefined) {
    node.status = "done";
  } else {
    // A close with no preceding start in this section (e.g. created and closed
    // before any start was observed): still surface it as a done node.
    section.steps.set(t.id, {
      ticket_id: t.id,
      title: t.id,
      status: "done",
      summary: null,
      narration: null,
      is_carryover: false,
      is_frontier: false,
      events: [],
    });
    section.step_order.push(t.id);
    section.entries.push({ kind: "step", id: t.id });
  }
  if (section.current_step_id === t.id) section.current_step_id = null;
}

function routeMessage(section: SectionBuilder, e: AssistantMessageEvent): void {
  const step_id = section.current_step_id;
  section.placements.push({ event: e, step_id });
  section.entries.push({ kind: "event", event: e, step_id });
  if (step_id !== null) {
    section.steps.get(step_id)!.events.push(e);
  }
}

/** Ids of steps still open (active, not done) at the end of a section, in
 *  first-appearance order -- the carryover set. */
function openStepsAtEnd(section: SectionBuilder): string[] {
  return section.step_order.filter((id) => section.steps.get(id)!.status === "active");
}

/** Finalize a section: pull out the trailing reply, attribute narration, join
 *  enrichment, append the pending roster, and emit items in transcript order. */
function finalizeSection(
  section: SectionBuilder,
  enrichment: Map<string, StepEnrichment>,
  agentIsIdle: boolean,
  is_tail: boolean,
): SectionView {
  // 1. Trailing reply: prose after the last real-work placement.
  let lastWorkIdx = -1;
  for (let i = 0; i < section.placements.length; i++) {
    if (isWork(section.placements[i].event)) lastWorkIdx = i;
  }
  const trailingIds = new Set<string>();
  const trailing_reply: AssistantMessageEvent[] = [];
  for (let i = lastWorkIdx + 1; i < section.placements.length; i++) {
    const p = section.placements[i];
    if (isProse(p.event)) {
      trailing_reply.push(p.event);
      trailingIds.add(p.event.event_id);
      // Remove promoted prose from its step's grouped events.
      if (p.step_id !== null) {
        const node = section.steps.get(p.step_id);
        if (node !== undefined) node.events = node.events.filter((ev) => ev.event_id !== p.event.event_id);
      }
    }
  }

  // 2. Narration: latest in-step prose followed by more work in the same step.
  for (const id of section.step_order) {
    const node = section.steps.get(id)!;
    let narration: string | null = null;
    for (let i = 0; i < node.events.length; i++) {
      const ev = node.events[i];
      if (!isProse(ev)) continue;
      const followedByWork = node.events.slice(i + 1).some(isWork);
      if (followedByWork) narration = ev.text;
    }
    node.narration = narration;
  }

  // 3. Frontier: the live step the agent is on -- only on the tail section,
  //    only when not idle, only the current open step.
  const frontierId = is_tail && !agentIsIdle ? section.current_step_id : null;

  // 4. Join enrichment onto each node.
  for (const id of section.step_order) {
    const node = section.steps.get(id)!;
    const enrich = enrichment.get(id);
    if (enrich !== undefined) {
      node.title = enrich.title || node.title;
      if (node.status === "done") node.summary = enrich.summary;
    }
    node.is_frontier = node.ticket_id === frontierId && node.status === "active";
  }

  // 5. Build timeline items by walking the entry skeleton in transcript order.
  //    Each step node is emitted at its first appearance -- its transition --
  //    so a step renders at its transcript position even if it carried no work.
  //    Routed events live in their step node (already emitted) or coalesce into
  //    an inline ungrouped run when no step was open. Carryover steps were
  //    recorded as the section's first entries, so they lead the timeline.
  const items: TimelineItem[] = [];
  const emittedSteps = new Set<string>();
  let ungrouped: AssistantMessageEvent[] = [];
  let ungroupedKey = 0;
  const flushUngrouped = (): void => {
    if (ungrouped.length > 0) {
      items.push({ kind: "ungrouped", key: `${section.key}-ung-${ungroupedKey++}`, events: ungrouped });
      ungrouped = [];
    }
  };

  const chipsAfter = new Map<number, UserMessageEvent[]>();
  for (const c of section.chips) {
    const arr = chipsAfter.get(c.after) ?? [];
    arr.push(c.event);
    chipsAfter.set(c.after, arr);
  }
  const emitChips = (afterIdx: number): void => {
    for (const c of chipsAfter.get(afterIdx) ?? []) {
      flushUngrouped();
      items.push({ kind: "chip", event: c });
    }
  };

  // A chip that fires before any entry (after === -1) renders at the top.
  emitChips(-1);

  for (let i = 0; i < section.entries.length; i++) {
    const entry = section.entries[i];
    if (entry.kind === "step") {
      flushUngrouped();
      if (!emittedSteps.has(entry.id)) {
        items.push({ kind: "step", step: section.steps.get(entry.id)! });
        emittedSteps.add(entry.id);
      }
    } else if (!trailingIds.has(entry.event.event_id) && entry.step_id === null) {
      // An ungrouped event (no step open): coalesce into an inline run.
      // In-step events render inside the step node; trailing prose renders
      // below the timeline (see trailing_reply).
      ungrouped.push(entry.event);
    }
    emitChips(i);
  }
  flushUngrouped();

  // 6. Pending roster (tail section only): steps in enrichment that never
  //    started anywhere, as dashed placeholders at the tail, in tk `created`
  //    order. The lone timestamp read in this module, and only among pending.
  if (is_tail) {
    const seen = new Set(section.step_order);
    const pending = Array.from(enrichment.entries())
      .filter(([id, en]) => en.status === "open" && !seen.has(id))
      .sort((a, b) => a[1].created_at.localeCompare(b[1].created_at) || a[0].localeCompare(b[0]));
    for (const [id, en] of pending) {
      items.push({
        kind: "step",
        step: {
          ticket_id: id,
          title: en.title || id,
          status: "pending",
          summary: null,
          narration: null,
          is_carryover: false,
          is_frontier: false,
          events: [],
        },
      });
    }
  }

  return { user_event: section.user_event, key: section.key, items, trailing_reply };
}
