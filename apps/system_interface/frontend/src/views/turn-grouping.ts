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

// Alias for consumers that still reference the old name.
export type TaskInTurn = StepView;

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

/** Attribute narration to steps from body events. Latest text-only
 *  assistant_message in each step's window wins. Skips done steps
 *  (summary owns the slot) and settled steps (their text is promoted
 *  to final messages instead). */
export function attributeNarration(steps: StepView[], body_events: TranscriptEvent[]): void {
  for (const e of body_events) {
    if (e.type !== "assistant_message") continue;
    if (!e.text) continue;
    if (e.tool_calls && e.tool_calls.length > 0) continue;
    const containing = findContainingStep(e.timestamp, steps);
    if (containing === null) continue;
    if (containing.status === "done") continue;
    if (containing.is_settled) continue;
    containing.narration = e.text;
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

/**
 * Text-only assistant_messages that should render at the top level
 * rather than being absorbed into a step's narration slot.
 *
 *   - Outside every step's window -> always top level
 *   - Last text-only message when its step is done or settled -> promoted
 */
export function selectFinalMessages(body_events: TranscriptEvent[], steps: StepView[]): TranscriptEvent[] {
  const textOnly = body_events.filter(
    (ev) => ev.type === "assistant_message" && !!ev.text && !(ev.tool_calls && ev.tool_calls.length > 0),
  );
  if (textOnly.length === 0) return [];
  const result: TranscriptEvent[] = [];
  for (const ev of textOnly) {
    if (findContainingStep(ev.timestamp, steps) === null) {
      result.push(ev);
    }
  }
  const lastMsg = textOnly[textOnly.length - 1];
  const lastContaining = findContainingStep(lastMsg.timestamp, steps);
  if (
    lastContaining !== null &&
    (lastContaining.status === "done" || lastContaining.is_settled) &&
    !result.includes(lastMsg)
  ) {
    result.push(lastMsg);
  }
  result.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  return result;
}
