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

/** Build a StepView from a TaskRecord. */
export function makeStepView(record: TaskRecord, is_settled: boolean): StepView {
  const status: TaskUiStatus =
    record.final_status === "closed" ? "done" : record.final_status === "in_progress" ? "active" : "pending";
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

/** Where a top-level (promoted) text message sits relative to the timeline. */
export type MessagePosition = "leading" | "inter_step" | "trailing";

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

/** Timestamp of the last "stop boundary" for the backward reply scan: the
 *  latest tool activity OR step close in the section. Text strictly after
 *  this is the trailing reply run. Null when the section has neither. */
function lastReplyBoundary(body_events: TranscriptEvent[], steps: StepView[]): string | null {
  let last: string | null = null;
  const bump = (ts: string | null): void => {
    if (ts !== null && (last === null || ts > last)) last = ts;
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
 */
export function classifyTopLevelMessages(body_events: TranscriptEvent[], steps: StepView[]): PlacedMessages {
  const result: PlacedMessages = { leading: [], inter_step: [], trailing: [] };
  const textOnly = body_events.filter(isTextOnlyAssistant);
  if (textOnly.length === 0) return result;

  const sorted = sortedWindowedSteps(steps);
  const firstStart = sorted.length > 0 ? sorted[0].active_window_start : null;
  const boundary = lastReplyBoundary(body_events, steps);
  const isTrailing = (ts: string): boolean =>
    boundary !== null ? ts > boundary : firstStart !== null && ts >= firstStart;

  for (const ev of textOnly) {
    const ts = ev.timestamp;
    if (isTrailing(ts)) {
      result.trailing.push(ev);
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
