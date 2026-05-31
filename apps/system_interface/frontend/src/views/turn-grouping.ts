/**
 * Step model: fold task_event stream into renderable step state.
 *
 * Steps (tk step records) are the primary grouping unit for the progress
 * view. Each step has a time window (started_at -> closed_at) and
 * transcript events whose timestamps fall in that window belong to it.
 *
 * This module provides:
 *   - TaskRecord: folded per-ticket state from task_event events
 *   - StepView: renderable projection of a TaskRecord
 *   - Query functions for attributing events to steps
 *
 * The rendering layer (ChatPanel) partitions the event stream by
 * user-message boundaries and uses these utilities to populate each
 * partition. There is no formalized "turn" or "section" abstraction here.
 */

import type { TranscriptEvent, TaskEventStatus } from "../models/Response";
import { isStopHookFeedback } from "./user-message-classification";

export type TaskUiStatus = "pending" | "active" | "done";

const STATUS_RANK: Record<TaskEventStatus, number> = {
  open: 0,
  in_progress: 1,
  closed: 2,
};

/** Folded view of a tk ticket's full event history. */
export interface TaskRecord {
  ticket_id: string;
  title: string;
  created_at: string;
  started_at: string | null;
  closed_at: string | null;
  summary: string | null;
  final_status: TaskEventStatus;
  step: boolean;
  parent_id: string;
  assignee: string;
  first_observed_at: string;
}

/** A step as it should be rendered. */
export interface StepView {
  ticket_id: string;
  title: string;
  status: TaskUiStatus;
  summary: string | null;
  created_at: string;
  started_at: string | null;
  /** Inclusive lower bound of the active window for tool-call attribution. */
  active_window_start: string | null;
  /** Inclusive upper bound of the active window. null = still active. */
  active_window_end: string | null;
  is_step: boolean;
  parent_id: string;
  children: StepView[];
  /** Live status caption: latest text-only assistant_message in the
   *  step's active window. Null when no text has landed yet. */
  narration: string | null;
  /** True when this step is no longer actively being worked on.
   *  The UI uses this to swap the live spinner for a static icon. */
  is_settled: boolean;
}

/** Fold task_event events into per-ticket TaskRecord. Latest status
 *  (by STATUS_RANK) wins; transitions track each timestamp. */
export function buildTaskRecords(events: TranscriptEvent[]): Map<string, TaskRecord> {
  const records = new Map<string, TaskRecord>();
  for (const e of events) {
    if (e.type !== "task_event" || !e.ticket_id || !e.status) continue;
    const existing = records.get(e.ticket_id);
    if (existing === undefined) {
      records.set(e.ticket_id, {
        ticket_id: e.ticket_id,
        title: e.title ?? e.ticket_id,
        created_at: e.created_at || e.timestamp,
        started_at: e.status === "in_progress" ? e.timestamp : null,
        closed_at: e.status === "closed" ? e.timestamp : null,
        summary: e.status === "closed" ? (e.summary ?? null) : null,
        final_status: e.status,
        step: e.step ?? false,
        parent_id: e.parent_id ?? "",
        assignee: e.assignee ?? "",
        first_observed_at: e.timestamp,
      });
      continue;
    }

    if (e.status === "in_progress" && existing.started_at === null) {
      existing.started_at = e.timestamp;
    }
    if (e.status === "closed") {
      existing.closed_at = e.timestamp;
      if (e.summary !== undefined && e.summary !== null) {
        existing.summary = e.summary;
      }
    }
    if (STATUS_RANK[e.status] >= STATUS_RANK[existing.final_status]) {
      existing.final_status = e.status;
    }
    if (e.timestamp < existing.first_observed_at) {
      existing.first_observed_at = e.timestamp;
    }
    if (e.assignee !== undefined && e.assignee !== "") {
      existing.assignee = e.assignee;
    }
    if (e.parent_id !== undefined && e.parent_id !== "") {
      existing.parent_id = e.parent_id;
    }
    if (e.step !== undefined) {
      existing.step = e.step;
    }
  }
  return records;
}

/** A step's status *as of* a partition's end boundary.
 *
 *  `buildTaskRecords` folds a ticket's whole history into one record whose
 *  `final_status` is the LATEST status seen anywhere. Rendering that global
 *  status in an *earlier* partition is wrong: a step that was still
 *  in_progress at this partition's boundary, but closes in a later one,
 *  would retroactively show as "done" here -- and its future `closed_at`
 *  would leak backwards (moving the reply boundary, collapsing the
 *  end-of-turn reply). Clamp to the status that was actually true at
 *  `partition_end`. `partition_end === ""` is the open tail: no clamp. */
function statusAsOf(record: TaskRecord, partition_end: string): TaskEventStatus {
  if (partition_end === "") return record.final_status;
  if (record.closed_at !== null && record.closed_at < partition_end) return "closed";
  if (record.started_at !== null && record.started_at < partition_end) return "in_progress";
  return "open";
}

/** Build a StepView from a TaskRecord, clamped to `partition_end` (see
 *  `statusAsOf`). `partition_end === ""` (the default) renders the latest
 *  known status, for the open tail partition. */
export function makeStepView(record: TaskRecord, is_settled: boolean, partition_end: string = ""): StepView {
  const effective = statusAsOf(record, partition_end);
  const status: TaskUiStatus = effective === "closed" ? "done" : effective === "in_progress" ? "active" : "pending";
  return {
    ticket_id: record.ticket_id,
    title: record.title,
    status,
    summary: status === "done" ? record.summary : null,
    created_at: record.created_at,
    started_at: record.started_at,
    active_window_start: status === "pending" ? null : (record.started_at ?? record.created_at),
    active_window_end: status === "done" ? record.closed_at : null,
    is_step: record.step,
    parent_id: record.parent_id,
    children: [],
    narration: null,
    is_settled,
  };
}

/** True when a step record is active during a time window: either
 *  it was first observed in the window, or it was already active when
 *  the window started and hasn't closed before it. */
export function stepActiveInWindow(record: TaskRecord, start_ts: string, end_ts: string): boolean {
  const attribution_ts = record.first_observed_at || record.created_at;
  if (attribution_ts >= start_ts && (end_ts === "" || attribution_ts < end_ts)) {
    return true;
  }
  if (attribution_ts < start_ts) {
    if (record.closed_at === null || record.closed_at >= start_ts) {
      return true;
    }
  }
  return false;
}

/** Whether a step's first_observed_at precedes the given timestamp
 *  (i.e. it carried over from a prior partition). */
export function isStepCarryover(record: TaskRecord, partition_start: string): boolean {
  const attr = record.first_observed_at || record.created_at;
  return attr < partition_start;
}

/** Sort steps: carryovers first, then by started_at, then created_at. */
export function sortSteps(steps: StepView[], records: Map<string, TaskRecord>, partition_start: string): StepView[] {
  const byStart = (a: StepView, b: StepView) => {
    if (a.started_at !== null && b.started_at !== null) return a.started_at.localeCompare(b.started_at);
    if (a.started_at !== null) return -1;
    if (b.started_at !== null) return 1;
    return a.created_at.localeCompare(b.created_at);
  };
  const carry = steps.filter((s) => isStepCarryover(records.get(s.ticket_id)!, partition_start)).sort(byStart);
  const own = steps.filter((s) => !isStepCarryover(records.get(s.ticket_id)!, partition_start)).sort(byStart);
  return [...carry, ...own];
}

/** ticket_id of the live "frontier" step in a partition: the most
 *  recently-*started* step as of `partition_end`, whatever its status now.
 *  This is the step the agent is actually on -- the only one that may show a
 *  live spinner. Crucially the frontier is chosen across both in_progress and
 *  already-closed steps: once a *later* step has started (even one that has
 *  since finished), any earlier still-open step was left behind and must not
 *  keep spinning above it. When the frontier is a done step, no in_progress
 *  step matches it, so every lingering open step settles. Returns null when
 *  no step has started yet. */
function frontierStepId(records: TaskRecord[], partition_end: string): string | null {
  let frontier: TaskRecord | null = null;
  let frontierStart = "";
  for (const r of records) {
    if (statusAsOf(r, partition_end) === "open") continue; // not started as of this boundary
    const started = r.started_at ?? r.created_at;
    if (frontier === null || started > frontierStart) {
      frontier = r;
      frontierStart = started;
    }
  }
  return frontier === null ? null : frontier.ticket_id;
}

/** Build the sorted, partition-clamped StepViews for one section window.
 *
 *  - Status / summary / active window are clamped to `partition_end` so a
 *    step that closes in a *later* partition still renders as active here
 *    (see `statusAsOf`): a global close must not retroactively flip an
 *    earlier block to "done", nor leak its future `closed_at` into the
 *    earlier block's reply-boundary scan.
 *  - `is_settled` is computed per step, not once per partition: an active
 *    step shows the live spinner only when it is the frontier (the agent's
 *    current step) AND the partition is the still-running tail. A non-tail
 *    or idle partition settles every step; a superseded earlier step (a
 *    later step has since started) settles even in the running tail, so it
 *    can't spin above a step that already finished. */
export function buildSectionSteps(
  records: Map<string, TaskRecord>,
  partition_start: string,
  partition_end: string,
  partition_is_settled: boolean,
): StepView[] {
  const active = Array.from(records.values()).filter(
    (r) => r.step && stepActiveInWindow(r, partition_start, partition_end),
  );
  const frontier = frontierStepId(active, partition_end);
  const views = active.map((r) => {
    const effective = statusAsOf(r, partition_end);
    // is_settled only governs the active-step icon (spinner vs static ring);
    // done/pending steps carry false, matching the prior behavior.
    const settled = effective !== "in_progress" ? false : partition_is_settled || r.ticket_id !== frontier;
    return makeStepView(r, settled, partition_end);
  });
  return sortSteps(views, records, partition_start);
}

/** True when `e` is a text-only assistant message (prose, no tool calls). */
function isTextOnlyAssistant(e: TranscriptEvent): boolean {
  return e.type === "assistant_message" && !!e.text && !(e.tool_calls && e.tool_calls.length > 0);
}

/** True when `e` represents tool activity: an assistant message that issues
 *  one or more tool calls, or a tool_result coming back. */
function isToolActivity(e: TranscriptEvent): boolean {
  if (e.type === "tool_result") return true;
  return e.type === "assistant_message" && !!(e.tool_calls && e.tool_calls.length > 0);
}

/** Whether tool activity occurs later in the same step's window as `ts`. */
function hasLaterToolActivityInStep(
  ts: string,
  step: StepView,
  body_events: TranscriptEvent[],
  steps: StepView[],
): boolean {
  for (const e of body_events) {
    if (e.timestamp <= ts) continue;
    if (!isToolActivity(e)) continue;
    if (findContainingStep(e.timestamp, steps) === step) return true;
  }
  return false;
}

/** Attribute mid-work narration to steps. A step's narration is the latest
 *  text-only assistant message in its window that was *followed by tool
 *  activity* within the same window -- i.e. the agent spoke and then kept
 *  working, so the text is progress narration rather than a wrap-up reply.
 *
 *  This is decoupled from `is_settled`: an unclosed step that goes idle
 *  still surfaces its mid-work narration (rendered static, not shimmering).
 *  Done steps are skipped -- their close summary owns the caption slot. A
 *  trailing message not followed by any tool activity is left for the
 *  reply-detection / positional logic, never absorbed here. */
export function attributeNarration(steps: StepView[], body_events: TranscriptEvent[]): void {
  for (const e of body_events) {
    if (!isTextOnlyAssistant(e)) continue;
    const containing = findContainingStep(e.timestamp, steps);
    if (containing === null) continue;
    if (containing.status === "done") continue;
    if (!hasLaterToolActivityInStep(e.timestamp, containing, body_events, steps)) continue;
    containing.narration = e.text ?? null;
  }
}

// --- Internal helpers for step window queries ---

function collectSortedSteps(steps: StepView[]): StepView[] {
  const result: StepView[] = [];
  const visit = (s: StepView): void => {
    if (s.is_step && s.active_window_start !== null) result.push(s);
    for (const child of s.children) visit(child);
  };
  for (const s of steps) visit(s);
  result.sort((a, b) => (a.active_window_start ?? "").localeCompare(b.active_window_start ?? ""));
  return result;
}

function computeEffectiveEnd(step: StepView, sortedSteps: StepView[]): string | null {
  const idx = sortedSteps.indexOf(step);
  const nextStart = idx >= 0 && idx + 1 < sortedSteps.length ? sortedSteps[idx + 1].active_window_start : null;
  let effectiveEnd: string | null = step.active_window_end;
  if (nextStart !== null && (effectiveEnd === null || nextStart < effectiveEnd)) {
    effectiveEnd = nextStart;
  }
  return effectiveEnd;
}

/** Find the step whose active window contains `ts`. */
export function findContainingStep(ts: string, steps: StepView[]): StepView | null {
  const sorted = collectSortedSteps(steps);
  for (let i = 0; i < sorted.length; i++) {
    const step = sorted[i];
    const start = step.active_window_start!;
    if (ts < start) continue;
    const effectiveEnd = computeEffectiveEnd(step, sorted);
    if (effectiveEnd !== null && ts > effectiveEnd) continue;
    return step;
  }
  return null;
}

/** Events that fall inside a step's active window. Enforces the
 *  serial-step invariant when sibling steps are provided. Pulls in
 *  trailing tool_results whose tool_use was in-window. */
export function eventsInTaskWindow(
  task: StepView,
  body_events: TranscriptEvent[],
  tasks?: StepView[],
): TranscriptEvent[] {
  const start = task.active_window_start ?? "";
  if (start === "") return [];
  let end: string;
  if (tasks !== undefined && task.is_step) {
    const sortedSteps = collectSortedSteps(tasks);
    const effective = computeEffectiveEnd(task, sortedSteps);
    end = effective ?? "";
  } else {
    end = task.active_window_end ?? "";
  }
  const inWindow = body_events.filter((e) => {
    if (e.timestamp < start) return false;
    if (end !== "" && e.timestamp > end) return false;
    return true;
  });
  if (end === "") return inWindow;
  const inWindowCallIds = new Set<string>();
  for (const e of inWindow) {
    if (e.type === "assistant_message" && e.tool_calls) {
      for (const tc of e.tool_calls) {
        inWindowCallIds.add(tc.tool_call_id);
      }
    }
  }
  if (inWindowCallIds.size === 0) return inWindow;
  const trailingResults = body_events.filter(
    (e) => e.type === "tool_result" && e.timestamp > end && !!e.tool_call_id && inWindowCallIds.has(e.tool_call_id),
  );
  if (trailingResults.length === 0) return inWindow;
  return [...inWindow, ...trailingResults].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
}

/** An inter-step message plus the id of the step it interrupts before. */
export interface InterStepMessage {
  event: TranscriptEvent;
  /** ticket_id of the step whose node renders immediately after this block. */
  before_step_id: string;
}

/** Positionally-classified top-level messages for a section. */
export interface PlacedMessages {
  /** Prose emitted before the first step started -> above the timeline. */
  leading: TranscriptEvent[];
  /** Prose in a gap between a closed step and the next step's start ->
   *  interrupts the timeline inline at that point. */
  inter_step: InterStepMessage[];
  /** The user-facing reply (backward-scan run) -> below the timeline. */
  trailing: TranscriptEvent[];
}

/** Top-level steps that have an active window, sorted by window start. */
function sortedWindowedSteps(steps: StepView[]): StepView[] {
  return steps
    .filter((s) => s.active_window_start !== null)
    .slice()
    .sort((a, b) => (a.active_window_start ?? "").localeCompare(b.active_window_start ?? ""));
}

/** Timestamps of the stop-hook feedback messages in the section, sorted.
 *  Each one ends the agent's prior reply segment and starts a new one: a
 *  Stop hook fires when the agent thinks its turn is over, so a wrap-up reply
 *  written before it and a reply written after it are two distinct replies,
 *  detected independently. Skill expansions and ordinary chips are NOT
 *  segment boundaries -- they happen mid-work. */
function replySegmentBoundaries(body_events: TranscriptEvent[]): string[] {
  return body_events
    .filter((e) => e.type === "user_message" && isStopHookFeedback(e.content ?? ""))
    .map((e) => e.timestamp)
    .sort((a, b) => a.localeCompare(b));
}

/** The reply segment [start, end) that contains `ts`. start === "" means the
 *  pre-hook segment (no lower bound); end === "" means the final segment (no
 *  upper bound). */
function segmentFor(ts: string, boundaries: string[]): { start: string; end: string } {
  let start = "";
  let end = "";
  for (const b of boundaries) {
    if (b <= ts) {
      start = b;
    } else {
      end = b;
      break;
    }
  }
  return { start, end };
}

/** The last "stop boundary" for the backward reply scan *within one reply
 *  segment*: the latest tool activity OR step close in [seg_start, seg_end).
 *  Text strictly after this (and before the segment end) is that segment's
 *  trailing reply. Null when the segment has neither. Passing
 *  seg_start = seg_end = "" scans the whole section (the no-stop-hook case),
 *  matching the original single-boundary behavior. */
function segmentReplyBoundary(
  body_events: TranscriptEvent[],
  steps: StepView[],
  seg_start: string,
  seg_end: string,
): string | null {
  const inSegment = (ts: string): boolean => ts >= seg_start && (seg_end === "" || ts < seg_end);
  let last: string | null = null;
  const bump = (ts: string | null): void => {
    if (ts !== null && inSegment(ts) && (last === null || ts > last)) last = ts;
  };
  for (const e of body_events) {
    if (isToolActivity(e)) bump(e.timestamp);
  }
  for (const s of steps) {
    if (s.status === "done") bump(s.active_window_end);
  }
  return last;
}

type Located = { kind: "in_step" } | { kind: "gap"; before_step_id: string };

/** Locate a (non-leading, non-trailing) timestamp against the sorted step
 *  windows: inside a step's window, or in an inter-step gap (after a closed
 *  step, before the next step starts). */
function locate(ts: string, sorted: StepView[]): Located {
  for (let i = 0; i < sorted.length; i++) {
    const start = sorted[i].active_window_start!;
    if (ts < start) continue;
    const nextStart = i + 1 < sorted.length ? sorted[i + 1].active_window_start : null;
    // done step -> window ends at its close; open step -> extends to the
    // next step's start (an abandoned step) or stays open to the end.
    const end = sorted[i].active_window_end ?? nextStart;
    if (end === null || ts < end) return { kind: "in_step" };
    if (nextStart !== null && ts < nextStart) return { kind: "gap", before_step_id: sorted[i + 1].ticket_id };
  }
  // Past the last step's end with no successor: not a gap, leave in-step
  // (it would already be the trailing reply if it were after the boundary).
  return { kind: "in_step" };
}

/**
 * Classify the section's text-only assistant messages into top-level
 * placements, never hiding any under a step:
 *
 *   - **trailing**: the backward-scan reply -- text strictly after the last
 *     tool activity or step close. Rendered below the timeline.
 *   - **leading**: text before the first step started. Rendered above.
 *   - **inter_step**: text in a gap between a closed step and the next
 *     step's start. Interrupts the timeline inline before that next step.
 *
 * Text that falls inside a step's window and is *not* trailing stays in
 * the step (as narration / expandable body) and is not returned here.
 *
 * The backward reply scan is **per reply segment**, where a stop-hook
 * feedback message splits the section into segments. This makes the scan
 * robust to stop hooks: post-hook tool activity can no longer bury the
 * genuine pre-hook wrap-up reply, and a reply the agent writes *in response
 * to* a stop hook surfaces too rather than collapsing or replacing the
 * pre-hook one. Only the **final** segment's reply renders below the timeline
 * (trailing); a reply from an *earlier* segment is woven into the timeline at
 * its chronological position (inter_step), so the whole turn reads top-to-
 * bottom in time and the earlier reply stays visible instead of being yanked
 * to the bottom or collapsed under a step.
 */
export function classifyTopLevelMessages(body_events: TranscriptEvent[], steps: StepView[]): PlacedMessages {
  const result: PlacedMessages = { leading: [], inter_step: [], trailing: [] };
  const textOnly = body_events.filter(isTextOnlyAssistant);
  if (textOnly.length === 0) return result;

  const sorted = sortedWindowedSteps(steps);
  const firstStart = sorted.length > 0 ? sorted[0].active_window_start : null;
  const segmentBounds = replySegmentBoundaries(body_events);
  // The final reply segment begins at the last stop-hook (or at section start
  // when there are none). A reply detected before this point belongs to an
  // earlier segment and is woven in chronologically rather than rendered below.
  const finalSegmentStart = segmentBounds.length > 0 ? segmentBounds[segmentBounds.length - 1] : "";
  // Position a woven message before the first step that starts after it (or at
  // the timeline's end when none do) -- the same rule the stop-hook chip uses.
  const weaveBeforeStepId = (ts: string): string => {
    const next = sorted.find((s) => (s.active_window_start ?? "") > ts);
    return next ? next.ticket_id : "";
  };

  for (const ev of textOnly) {
    const ts = ev.timestamp;
    const segment = segmentFor(ts, segmentBounds);
    const boundary = segmentReplyBoundary(body_events, steps, segment.start, segment.end);
    const isReplyRun = boundary !== null ? ts > boundary : firstStart !== null && ts >= firstStart;
    if (isReplyRun) {
      // The final segment's reply renders below the timeline; an earlier
      // segment's reply is woven in at its chronological spot (never collapsed
      // -- it was detected as a reply, so it must stay visible).
      if (ts >= finalSegmentStart) {
        result.trailing.push(ev);
      } else {
        result.inter_step.push({ event: ev, before_step_id: weaveBeforeStepId(ts) });
      }
      continue;
    }
    if (firstStart === null || ts < firstStart) {
      result.leading.push(ev);
      continue;
    }
    const located = locate(ts, sorted);
    if (located.kind === "gap") {
      result.inter_step.push({ event: ev, before_step_id: located.before_step_id });
    }
    // kind === "in_step": stays in the step, not promoted.
  }
  return result;
}

/** A stop-hook chip plus the id of the step it should render immediately
 *  before in the timeline. `before_step_id === ""` means render after the
 *  last step (the hook fired after all step activity in the section). */
export interface PlacedChip {
  event: TranscriptEvent;
  before_step_id: string;
}

/** Position each stop-hook feedback message at its chronological spot in the
 *  section's timeline: immediately before the first step that *starts after*
 *  the hook fired, or after the last step when none do. The chat panel emits
 *  the chip mid-section but renders the whole section as one progress block,
 *  so without this the chip floats above the entire turn instead of sitting
 *  where the hook actually interrupted the work. */
export function placeStopHookChips(body_events: TranscriptEvent[], steps: StepView[]): PlacedChip[] {
  const sorted = sortedWindowedSteps(steps);
  const placed: PlacedChip[] = [];
  for (const e of body_events) {
    if (e.type !== "user_message" || !isStopHookFeedback(e.content ?? "")) continue;
    const next = sorted.find((s) => (s.active_window_start ?? "") > e.timestamp);
    placed.push({ event: e, before_step_id: next ? next.ticket_id : "" });
  }
  return placed;
}
