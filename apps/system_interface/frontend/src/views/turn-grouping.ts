/**
 * Step grouping: a single in-order walk of the transcript.
 *
 * The progress view is a pure frontend for the agent session transcript.
 * BOTH structure and decoration come from the transcript -- there is no
 * side-channel.
 *
 * - *Structure* (which steps exist, their order, their open/close transitions,
 *   which events belong to which step) is read from transcript position. tk
 *   lifecycle commands (`tk create/start/close`) appear as Bash tool calls;
 *   their results carry the canonical id and status (`Updated <id> -> <status>`),
 *   which is all the structure we need.
 * - *Decoration* (titles, close summaries) is read from the machine-readable
 *   lines tk prints on stdout: `Created <id>: <title>` (create),
 *   `tk-step <id> title: <title>` (start/close), and
 *   `tk-step <id> summary: <summary>` (close). These are parsed in one global
 *   pass over all events into a per-id decoration map, so a carried-over node
 *   resolves its title from the `tk start`/create line in an earlier turn.
 * - For *historical* transcripts that predate those output lines, a best-effort
 *   input-preview fallback recovers titles/summaries from the tk command inputs
 *   (`tk create --step "Title"`, `tk close <id> "summary"`). It is allowed to be
 *   imperfect; it serves a frozen corpus only.
 *
 * The walk maintains a single "current open step": events while a step is open
 * group under it; events while none is open fall into an ungrouped run rendered
 * inline (the same plain-chat path used for turns with no steps). When a step
 * closes, any prose it spoke after its last work is ejected into the ungrouped
 * inline stream right after the step node, so a closing remark is promoted out
 * of the step rather than buried in it. The turn's wrap-up reply is simply the
 * final run of ungrouped prose, rendered below the timeline.
 *
 * A step still open when the next user message arrives carries over: it
 * re-renders at the top of the new turn, while the prior turn's node freezes at
 * its last-known state.
 *
 * One message is never grouped under a step: an agent permission request. It
 * is lifted out into a dedicated inline break (the `permission` timeline item)
 * so the user always sees it and can respond without expanding a step. The step
 * it interrupted stays open, so work resumed afterwards keeps grouping under it.
 * When the user later grants or denies the request, the app injects a plain
 * notification user message. The walk reads its verdict onto the request's card
 * and treats the notification as a turn boundary (the agent blocked on the
 * request and is now resuming), so any open step carries over and continues in
 * the new turn rather than the same node resuming beneath the card. The raw text
 * isn't shown (its verdict is on the card). The notification carries no request
 * id, so it resolves the oldest still-open request (the agent blocks on a
 * request until answered, so in practice only one is open at a time).
 *
 * This module reads no timestamps. Pending placeholders are ordered by
 * transcript position; grouping and the positioning of any transitioned step
 * read transcript order alone.
 */

import type {
  TranscriptEvent,
  AssistantMessageEvent,
  UserMessageEvent,
  ToolResultEvent,
  ToolCall,
} from "../models/Response";
import type { PermissionResolution } from "./message-classification";
import {
  isNonBoundaryUserMessage,
  isPermissionRequestCall,
  isStopHookFeedback,
  parsePermissionResolution,
} from "./message-classification";

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
  /** Latest in-step prose, shown as the live caption under the step. For a
   *  non-frontier step this is the latest prose followed by more work in the
   *  same step (trailing closing prose having been ejected); for the live
   *  frontier step it is the last thing the agent said, so a just-spoken line
   *  shows even before the next tool call. */
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
  /** Real work and/or prose with no step open: pre-step work, or a step's
   *  ejected closing prose. Rendered inline, exactly like a no-steps turn. */
  | { kind: "ungrouped"; key: string; events: AssistantMessageEvent[] }
  /** An agent permission request, lifted out of any open step so it always
   *  renders inline as a thread-breaking block. The user must be able to see and
   *  act on it without expanding a step. `resolution` is set once a later
   *  granted/denied notification is correlated to this request (see
   *  buildSections); null while still awaiting a decision. */
  | { kind: "permission"; event: AssistantMessageEvent; resolution: PermissionResolution | null }
  /** A non-boundary user message shown inline (e.g. a stop-hook chip). */
  | { kind: "chip"; event: UserMessageEvent };

/** A turn: the user message, its timeline, and the wrap-up reply below it. */
export interface SectionView {
  /** The boundary user message that opened this section, or null for content
   *  that precedes the first user message. */
  user_event: UserMessageEvent | null;
  key: string;
  items: TimelineItem[];
  /** The final run of ungrouped prose: the user-facing reply, rendered below
   *  the timeline. */
  trailing_reply: AssistantMessageEvent[];
}

/** Detects a `tk`/`ticket` lifecycle invocation at the START of a Bash tool
 *  call's command, so a pure tk call (the enforced shape for `tk start`/`close`,
 *  and the batched literal-id `tk create --step ...` form) is hidden from the
 *  rendered output. A command that merely mentions a tk verb later (e.g.
 *  `git commit -m "tk close ..."`) is NOT misclassified. `super` is the
 *  plugin-bypassing form. */
const TK_LIFECYCLE_RE = /"command"\s*:\s*"\s*(?:tk|ticket)\s+(?:super\s+)?(?:create|start|close)\b/;

/** A status transition line printed by tk on every state change:
 *  `Updated <id> -> <status>` (see vendor/tk/ticket). Global so a batched
 *  command that flips several tickets is read in order. */
const TK_UPDATED_RE = /Updated\s+(\S+)\s+->\s+(open|in_progress|closed)/g;

/** A step id (minted by `tk create --step`) carries a literal `-step-` segment,
 *  e.g. `cod-step-f1zl`; a regular ticket id has none (`cod-f1zl`). The walk
 *  uses this to recognise a step from its transition id alone, so a regular
 *  ticket the agent picked up (which prints the same `Updated <id> -> <status>`
 *  line) is never minted as a phantom timeline node. */
const STEP_ID_RE = /-step-[a-z0-9]+$/;

/** Decoration output lines tk prints on stdout. The format is defined in
 *  `vendor/tk/ticket` (cmd_create/start/close) and is also protected from
 *  truncation by the backend parser (`session_parser.py`); keep all three in
 *  sync. The id is required to carry the `-step-` segment so a stray
 *  "Created ..." line in other tool output (e.g. a scaffolder's "Created lib
 *  at ...") is never mistaken for a step. */
const CREATED_RE = /^Created (\S+-step-[a-z0-9]+): (.*)$/gm;
const TK_STEP_TITLE_RE = /^tk-step (\S+) title: (.*)$/gm;
const TK_STEP_SUMMARY_RE = /^tk-step (\S+) summary: (.*)$/gm;

/** Historical input fallback: titles from `tk create --step "Title"` and
 *  summaries from `tk close <id> "summary"`, pulled from the (un-truncated) tk
 *  command. `\b` anchors the verb to a word boundary so a tk mention inside
 *  another token is not matched. Both single- and double-quoted arguments are
 *  accepted (agents use both); the title/summary is whichever group matched. */
const CREATE_TITLE_RE = /\b(?:tk|ticket)\s+(?:super\s+)?create\b[^"']*?(?:"([^"]*)"|'([^']*)')/g;
const CLOSE_SUMMARY_RE = /\b(?:tk|ticket)\s+(?:super\s+)?close\s+(\S+)\s+(?:"([^"]*)"|'([^']*)')/g;

/** Cheap gate: skip the JSON.parse + fallback for a Bash input that cannot be a
 *  tk create/close at all. Tested against the raw input_preview. */
const TK_CREATE_OR_CLOSE_RAW = /(?:tk|ticket)\s+(?:super\s+)?(?:create|close)\b/;

function isStepId(id: string): boolean {
  return STEP_ID_RE.test(id);
}

/** True when a tool call is a tk lifecycle command (consumed as a structural
 *  marker, not rendered as work). Restricted to Bash calls whose command
 *  begins with the tk verb (see TK_LIFECYCLE_RE). */
function isTkLifecycleCall(tc: ToolCall): boolean {
  return tc.tool_name === "Bash" && TK_LIFECYCLE_RE.test(tc.input_preview);
}

/** True when an assistant message issues a permission request. */
function hasPermissionRequest(e: AssistantMessageEvent): boolean {
  return e.tool_calls.some(isPermissionRequestCall);
}

/** The Bash command string for a tool call, or null if not a Bash call (or its
 *  input_preview is not parseable -- e.g. a truncated non-tk command). tk
 *  lifecycle inputs are exempt from input truncation, so they parse cleanly. */
function tkCommand(tc: ToolCall): string | null {
  if (tc.tool_name !== "Bash") return null;
  try {
    const obj = JSON.parse(tc.input_preview) as { command?: unknown };
    return typeof obj.command === "string" ? obj.command : null;
  } catch {
    return null;
  }
}

interface Decoration {
  title?: string;
  summary?: string;
}

interface DecorationResult {
  deco: Map<string, Decoration>;
  /** Ids known to be steps (seen in a create / tk-step line / create input). */
  knownSteps: Set<string>;
  /** Step ids in first-created (transcript) order, for the pending roster. */
  createdOrder: string[];
}

/** Build the per-id decoration map in one global pass over all events, so a
 *  carried-over node resolves its title from a `tk start`/create line in an
 *  earlier turn. Authoritative tk output lines win; the historical input
 *  fallback only fills gaps. */
function buildDecorationMap(events: TranscriptEvent[], toolResults: Map<string, ToolResultEvent>): DecorationResult {
  const deco = new Map<string, Decoration>();
  const knownSteps = new Set<string>();
  const createdOrder: string[] = [];

  const ensure = (id: string): Decoration => {
    let d = deco.get(id);
    if (d === undefined) {
      d = {};
      deco.set(id, d);
    }
    return d;
  };
  const registerCreated = (id: string): void => {
    knownSteps.add(id);
    if (!createdOrder.includes(id)) createdOrder.push(id);
  };

  for (const e of events) {
    if (e.type !== "assistant_message") continue;
    for (const tc of e.tool_calls) {
      const output = toolResults.get(tc.tool_call_id)?.output ?? "";

      // Authoritative new-format output lines.
      for (const m of output.matchAll(CREATED_RE)) {
        ensure(m[1]).title = m[2].trim();
        registerCreated(m[1]);
      }
      for (const m of output.matchAll(TK_STEP_TITLE_RE)) {
        ensure(m[1]).title = m[2].trim();
        knownSteps.add(m[1]);
      }
      for (const m of output.matchAll(TK_STEP_SUMMARY_RE)) {
        ensure(m[1]).summary = m[2].trim();
        knownSteps.add(m[1]);
      }

      // Historical input fallback (fills only what the output lines did not).
      if (!TK_CREATE_OR_CLOSE_RAW.test(tc.input_preview)) continue;
      const command = tkCommand(tc);
      if (command === null) continue;
      applyInputFallback(command, output, ensure, registerCreated, knownSteps);
    }
  }
  return { deco, knownSteps, createdOrder };
}

/** Recover decoration from a tk command's input (historical transcripts):
 *  - create titles: zipped positionally onto the step ids echoed in the output
 *    (the create command captured the id into a shell var, so id and title only
 *    pair by order). Skipped unless the counts match.
 *  - close summaries: id and summary are both in the command. */
function applyInputFallback(
  command: string,
  output: string,
  ensure: (id: string) => Decoration,
  registerCreated: (id: string) => void,
  knownSteps: Set<string>,
): void {
  const titles = [...command.matchAll(CREATE_TITLE_RE)].map((m) => m[1] ?? m[2]);
  if (titles.length > 0) {
    const ids = output.split(/[^A-Za-z0-9_-]+/).filter((t) => STEP_ID_RE.test(t));
    if (ids.length === titles.length) {
      for (let i = 0; i < ids.length; i++) {
        const d = ensure(ids[i]);
        if (d.title === undefined) d.title = titles[i].trim();
        registerCreated(ids[i]);
      }
    }
  }
  for (const m of command.matchAll(CLOSE_SUMMARY_RE)) {
    const id = m[1];
    if (!isStepId(id)) continue; // a regular ticket close is not a step
    const d = ensure(id);
    if (d.summary === undefined) d.summary = (m[2] ?? m[3]).trim();
    knownSteps.add(id);
  }
}

interface ParsedMessage {
  /** Start/close transitions this message caused, in order. (Creates are not
   *  positioned here -- pending steps come from the created roster.) */
  transitions: { id: string; status: "in_progress" | "closed" }[];
  /** The renderable remainder: the message stripped of its tk lifecycle calls.
   *  Null when nothing renderable remains (a pure tk command). */
  render: AssistantMessageEvent | null;
}

/** Split an assistant message into the tk transitions it caused and the
 *  renderable remainder (text + non-tk tool calls). */
function parseMessage(e: AssistantMessageEvent, toolResults: Map<string, ToolResultEvent>): ParsedMessage {
  // Transitions are read from EVERY tool call's output -- the
  // `Updated <id> -> <status>` line is specific enough that a genuine
  // transition is never missed, even if the command form isn't recognised as a
  // tk lifecycle call (so e.g. `cd x && tk close s1` still closes the step).
  const transitions: { id: string; status: "in_progress" | "closed" }[] = [];
  for (const tc of e.tool_calls) {
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

  // Only a recognised, pure tk lifecycle call is hidden from the rendered
  // output. Anything else -- including a command that merely mentions a tk verb
  // -- renders as normal work, so real work is never silently dropped.
  const realCalls = e.tool_calls.filter((tc) => !isTkLifecycleCall(tc));
  if (realCalls.length === e.tool_calls.length) {
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

/** An ordered skeleton entry recorded as the transcript is walked. A `step`
 *  entry marks where a step node first appears -- its first transition (an
 *  open, or a close with no prior open) -- so the node is positioned by that
 *  transition even when the step carries no work. An `event` entry is a routed
 *  assistant message. Timeline items are rebuilt from these in transcript
 *  order. */
type SectionEntry =
  | { kind: "step"; id: string }
  /** A permission request, lifted out of any open step to render inline as a
   *  visible break (see hasPermissionRequest / the `permission` TimelineItem). */
  | { kind: "permission"; event: AssistantMessageEvent }
  | { kind: "event"; event: AssistantMessageEvent; step_id: string | null };

interface SectionBuilder {
  user_event: UserMessageEvent | null;
  key: string;
  /** Step nodes in first-appearance (transcript) order. */
  steps: Map<string, StepNode>;
  step_order: string[];
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
    entries: [],
    chips: [],
    current_step_id: null,
  };
}

/** Walk the visible transcript into ordered sections. `toolResults` resolves tk
 *  command outputs (and is reused by the renderer). `agentIsIdle` settles the
 *  spinner on the tail section. All decoration is derived from the transcript;
 *  there is no enrichment argument. */
export function buildSections(
  events: TranscriptEvent[],
  toolResults: Map<string, ToolResultEvent>,
  agentIsIdle: boolean,
): SectionView[] {
  const { deco, knownSteps, createdOrder } = buildDecorationMap(events, toolResults);

  const builders: SectionBuilder[] = [];
  let current: SectionBuilder | null = null;
  // Steps open at the end of the prior section, to re-open as carryover.
  let carryover: string[] = [];
  // Permission requests awaiting a decision, in transcript (creation) order, by
  // the event id of the message that issued each. A granted/denied notification
  // carries no request id, so it resolves the oldest still-open request -- the
  // agent blocks on a request until it is answered, so in practice only one is
  // open at a time. Resolutions are keyed by the resolved request's event id.
  const unresolvedPermissions: string[] = [];
  const resolutions = new Map<string, PermissionResolution>();

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
      // A granted/denied notification for an earlier permission request. Record
      // the verdict against the oldest open request (it reflects on that
      // request's card, not as a user prompt), then treat the notification as
      // the turn boundary it naturally is: the agent blocked on the request and
      // is now resuming, so close the current section -- carrying any open step
      // over -- and open a fresh one. The step then continues in the normal
      // carryover way rather than the same node resuming inline beneath the
      // card. The raw text is not shown (its verdict is on the card), so the new
      // section has no user bubble. If no request is open to claim it (e.g. the
      // request scrolled out of the visible transcript), fall through and let it
      // render as an ordinary user message.
      const resolution = parsePermissionResolution(e.content ?? "");
      if (resolution !== null && unresolvedPermissions.length > 0) {
        const resolvedEventId = unresolvedPermissions.shift() as string;
        resolutions.set(resolvedEventId, resolution);
        carryover = current === null ? [] : openStepsAtEnd(current);
        current = ensureSection(null, `section-after-${e.event_id}`);
        continue;
      }
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
      // Apply transitions in transcript order so each step node lands at its
      // real position -- a batched `tk close a && tk start b` must keep a's node
      // before b's. Then route this message's content to the step it belongs
      // to: the step it opened (work shares a message with its `tk start`), or
      // else the step that was current before the message (work shares a
      // message with a `tk close` -- it stays in the closing step).
      const stepBefore = current.current_step_id;
      let lastOpened: string | null = null;
      for (const t of parsed.transitions) {
        // Only step records render in the timeline. A `tk start`/`tk close` on a
        // regular ticket the agent picked up prints the same
        // `Updated <id> -> <status>` line as a step does; skip it (it is neither
        // a `-step-` id nor a known step) rather than mint a phantom node titled
        // with the raw ticket id. Any work batched with that command stays in
        // the step that was already open (or renders inline if none was).
        if (!isStepId(t.id) && !knownSteps.has(t.id)) continue;
        applyTransition(current, t);
        if (t.status === "in_progress") lastOpened = t.id;
      }
      if (parsed.render !== null && (parsed.render.text || parsed.render.tool_calls.length > 0)) {
        if (hasPermissionRequest(parsed.render)) {
          // A permission request breaks out of any open step: it must always be
          // directly visible, never collapsed inside a step node. The step stays
          // open (current_step_id is untouched), so work resumed after the user
          // responds keeps grouping under it.
          current.entries.push({ kind: "permission", event: parsed.render });
          // Track it as awaiting a decision so a later granted/denied
          // notification can be correlated back to this card by order.
          unresolvedPermissions.push(parsed.render.event_id);
        } else {
          routeMessage(current, parsed.render, lastOpened ?? stepBefore);
        }
      }
      continue;
    }
    // tool_result events are resolved by id via toolResults; no routing needed.
  }

  // Pending roster: created steps that never transitioned anywhere, in
  // transcript (creation) order.
  const transitioned = new Set<string>();
  for (const b of builders) for (const id of b.step_order) transitioned.add(id);
  const pending: { id: string; title: string }[] = createdOrder
    .filter((id) => !transitioned.has(id))
    .map((id) => ({ id, title: deco.get(id)?.title ?? id }));

  const lastBuilder = builders[builders.length - 1];
  return builders.map((b) =>
    finalizeSection(b, deco, resolutions, b === lastBuilder ? pending : [], agentIsIdle, b === lastBuilder),
  );
}

/** Open (or re-open) a step node as the current step. */
function openStep(section: SectionBuilder, id: string, is_carryover: boolean): void {
  const existing = section.steps.get(id);
  if (existing === undefined) {
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
  } else if (existing.status === "done") {
    // Re-opened (a `tk start` on a previously-closed id in this section): it is
    // active again, so it must not keep showing as done/settled.
    existing.status = "active";
    existing.summary = null;
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

function routeMessage(section: SectionBuilder, e: AssistantMessageEvent, step_id: string | null): void {
  section.entries.push({ kind: "event", event: e, step_id });
  if (step_id !== null) {
    section.steps.get(step_id)?.events.push(e);
  }
}

/** Ids of steps still open (active, not done) at the end of a section, in
 *  first-appearance order -- the carryover set. */
function openStepsAtEnd(section: SectionBuilder): string[] {
  return section.step_order.filter((id) => section.steps.get(id)!.status === "active");
}

/** Finalize a section: eject each step's closing prose, pull out the trailing
 *  reply, attribute narration, join decoration, append the pending roster, and
 *  emit items in transcript order. */
function finalizeSection(
  section: SectionBuilder,
  deco: Map<string, Decoration>,
  resolutions: Map<string, PermissionResolution>,
  pending: { id: string; title: string }[],
  agentIsIdle: boolean,
  is_tail: boolean,
): SectionView {
  // The live frontier step (the open step the agent is actively on) -- the only
  // one that may show a spinner. Computed up front because the live step is
  // treated specially below: prose it just spoke is in-flight narration, not a
  // closing remark, since the step has not closed.
  const frontierId = is_tail && !agentIsIdle ? section.current_step_id : null;

  // 1. Ejection: prose spoken inside a step AFTER its last work (so it is NOT
  //    narration, which is prose *followed* by more work in the same step). It
  //    is the step's closing remark -- ejected from the step so it renders in
  //    the ungrouped inline stream right after the step node, rather than buried
  //    inside it. (If it is the last thing in the section it becomes the
  //    trailing reply below; see step 2.) The live frontier step is exempt: it
  //    has not closed, so its trailing prose is in-flight narration shown as a
  //    caption under the step (see step 3), not a closing remark.
  const ejectedIds = new Set<string>();
  for (const id of section.step_order) {
    if (id === frontierId) continue;
    const node = section.steps.get(id)!;
    let lastWorkIdx = -1;
    for (let i = 0; i < node.events.length; i++) if (isWork(node.events[i])) lastWorkIdx = i;
    for (let i = lastWorkIdx + 1; i < node.events.length; i++) {
      if (isProse(node.events[i])) ejectedIds.add(node.events[i].event_id);
    }
    if (ejectedIds.size > 0) node.events = node.events.filter((ev) => !ejectedIds.has(ev.event_id));
  }

  // 2. Trailing reply: the final run of ungrouped prose -- prose entries after
  //    the reply boundary (the latest of the last real work and the last step
  //    appearance). Including the last step appearance keeps a closing remark
  //    promoted to an inline break the moment the next step starts, rather than
  //    sinking below the timeline. These render below the timeline (not inline).
  let lastWorkEntryIdx = -1;
  let lastStepEntryIdx = -1;
  for (let i = 0; i < section.entries.length; i++) {
    const en = section.entries[i];
    // A permission request is real (non-tk) activity, so it acts as a reply
    // boundary just like a work event -- prose before a trailing permission
    // request stays inline at its spot, not hoisted below the timeline as a
    // reply.
    if (en.kind === "permission") lastWorkEntryIdx = i;
    else if (en.kind === "event" && isWork(en.event)) lastWorkEntryIdx = i;
    if (en.kind === "step") lastStepEntryIdx = i;
  }
  const replyBoundary = Math.max(lastWorkEntryIdx, lastStepEntryIdx);
  const trailingIds = new Set<string>();
  const trailing_reply: AssistantMessageEvent[] = [];
  for (let i = replyBoundary + 1; i < section.entries.length; i++) {
    const en = section.entries[i];
    if (en.kind !== "event" || !isProse(en.event)) continue;
    // Prose the agent just spoke inside the live frontier step is that step's
    // in-flight narration (a caption under the still-spinning step), not the
    // turn's wrap-up reply -- the step has not closed, so there is no reply yet.
    if (en.step_id !== null && en.step_id === frontierId) continue;
    trailing_reply.push(en.event);
    trailingIds.add(en.event.event_id);
  }

  // 3. Narration: the latest in-step prose -- the live caption under the step.
  //    For a non-frontier step this is the latest prose followed by more work in
  //    the same step (its trailing closing prose was already ejected in step 1).
  //    For the live frontier step (not ejected) it is simply the last thing the
  //    agent said, so a just-spoken line shows as a caption even before the next
  //    tool call.
  for (const id of section.step_order) {
    const node = section.steps.get(id)!;
    let narration: string | null = null;
    for (const ev of node.events) {
      if (isProse(ev)) narration = ev.text;
    }
    node.narration = narration;
  }

  // 4. Join decoration onto each node. A node with no decoration entry keeps the
  //    raw id as its title and has no summary (its create/tk-step lines scrolled
  //    out of the loaded window -- an accepted loss for very old steps).
  for (const id of section.step_order) {
    const node = section.steps.get(id)!;
    const d = deco.get(id);
    if (d?.title) node.title = d.title;
    if (node.status === "done") node.summary = d?.summary ?? null;
    node.is_frontier = node.ticket_id === frontierId && node.status === "active";
  }

  // 5. Build timeline items by walking the entry skeleton in transcript order.
  //    Each step node is emitted at its first appearance (its transition), so a
  //    step renders at its transcript position even if it carried no work. An
  //    ungrouped event (no step open) or an ejected closing-prose event coalesces
  //    into an inline ungrouped run; an in-step event lives inside its node.
  //    Carryover steps were recorded as the section's first entries, so they
  //    lead the timeline.
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
    } else if (entry.kind === "permission") {
      // A permission break ends any in-flight ungrouped run and stands as its
      // own always-visible item at its transcript position. Attach the verdict
      // if a later notification resolved this request.
      flushUngrouped();
      items.push({
        kind: "permission",
        event: entry.event,
        resolution: resolutions.get(entry.event.event_id) ?? null,
      });
    } else if (trailingIds.has(entry.event.event_id)) {
      // Trailing reply: rendered below the timeline (see trailing_reply).
    } else if (entry.step_id === null || ejectedIds.has(entry.event.event_id)) {
      // An ungrouped event (no step open) or a step's ejected closing prose:
      // coalesce into an inline run. In-step events render inside the step node.
      ungrouped.push(entry.event);
    }
    emitChips(i);
  }
  flushUngrouped();

  // 6. Pending roster (tail section only): created steps that never started, as
  //    dashed placeholders at the tail, in transcript order.
  if (is_tail) {
    for (const p of pending) {
      items.push({
        kind: "step",
        step: {
          ticket_id: p.id,
          title: p.title,
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
