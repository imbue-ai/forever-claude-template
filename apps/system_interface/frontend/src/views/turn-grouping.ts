/**
 * Group transcript events into turns and attribute tasks.
 *
 * A "turn" starts at a user_message and ends at the next user_message
 * (or now). Within a turn we collect:
 *   - The user_message itself
 *   - All assistant_message + tool_result events whose timestamp falls
 *     in the turn's window
 *   - Tasks attributed to this turn (created during this turn's window
 *     OR carried over from a prior turn while still unfinished)
 *
 * Task attribution is two-step:
 *   1. Fold task_event events by ticket_id into TaskInfo (the latest
 *      status wins; closed > in_progress > open). Track each transition
 *      timestamp (created_at / started_at / closed_at).
 *   2. Each task is "owned" by the turn whose window contains its
 *      created_at. Tasks that are still open at the END of their owning
 *      turn appear ALSO in every subsequent turn as a carryover entry,
 *      up to and including the turn during which they get closed. The
 *      owning turn renders the task in its state-as-of-turn-end
 *      (frozen); each carryover-receiving turn renders the same task in
 *      its state-as-of-that-turn-end. This keeps a long-running ticket
 *      visible while the user replies (e.g. granting a permission)
 *      mid-task rather than leaving the new turn with only raw tool
 *      calls and no progress block.
 */

import type { TranscriptEvent, TaskEventStatus } from "../models/Response";
import { isNonBoundaryUserMessage } from "./user-message-classification";

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
  /** Final status seen so far across all events. */
  final_status: TaskEventStatus;
}

/** A task as it should be rendered inside a specific turn. */
export interface TaskInTurn {
  ticket_id: string;
  title: string;
  /** UI-mapped status of the task as of the END of THIS turn. */
  status: TaskUiStatus;
  /** Summary text only when status === "done" (rendered under the task). */
  summary: string | null;
  /** True if this task was first created in a prior turn. Used internally
   *  for ordering; the UI distinguishes "in flight" from "live" via
   *  continues_forward rather than this flag. */
  is_carryover: boolean;
  /** True when this task is still open at the end of this turn AND a
   *  later turn exists. The UI uses this to swap the live spinner for a
   *  frozen "in flight" icon and to attach a "continued in next turn"
   *  badge -- because from the user's vantage point looking at a past
   *  turn, the work is not actively spinning in that turn anymore. */
  continues_forward: boolean;
  /** Timestamp the ticket was created. Used for ordering tasks within a
   *  turn (so a still-pending task created later than an active task
   *  renders below it, not above). */
  created_at: string;
  /** Inclusive lower bound of the active window for tool-call attribution. */
  active_window_start: string | null;
  /** Inclusive upper bound of the active window. null = still active at
   *  end of turn. */
  active_window_end: string | null;
}

export interface Turn {
  user_event: TranscriptEvent;
  /** Inclusive: timestamp of the user_message itself. */
  start_ts: string;
  /** Exclusive: timestamp of the next user_message, or "" (treated as
   *  +infinity) if this is the latest turn. */
  end_ts: string;
  /** Assistant messages and tool_results inside the window, in order. */
  body_events: TranscriptEvent[];
  /** Tasks rendered inside this turn's progress block. Empty list means
   *  this is a "plain" turn (no progress UI). */
  tasks: TaskInTurn[];
}

/** Fold task_event events into per-ticket TaskRecord. Latest status
 *  (by STATUS_RANK) wins; transitions track each timestamp. */
export function buildTaskRecords(events: TranscriptEvent[]): Map<string, TaskRecord> {
  const records = new Map<string, TaskRecord>();
  for (const e of events) {
    if (e.type !== "task_event" || !e.ticket_id || !e.status) continue;
    const existing = records.get(e.ticket_id);
    if (existing === undefined) {
      // started_at is set only when an in_progress event is OBSERVED.
      // The watcher emits a single "closed" event on replay (the
      // historical in_progress timestamp is gone), in which case we
      // leave started_at null and the active window for tool-call
      // attribution falls back to created_at -- see makeTaskInTurn.
      records.set(e.ticket_id, {
        ticket_id: e.ticket_id,
        title: e.title ?? e.ticket_id,
        // `||` (not `??`) so an empty-string created_at -- which the
        // watcher emits for malformed tickets that lack a `created:`
        // frontmatter line -- falls back to the event's own timestamp.
        // An empty record.created_at would never satisfy the
        // `record.created_at >= turn.start_ts` window check in
        // buildTurns and the task would be silently dropped from every
        // turn.
        created_at: e.created_at || e.timestamp,
        started_at: e.status === "in_progress" ? e.timestamp : null,
        closed_at: e.status === "closed" ? e.timestamp : null,
        summary: e.status === "closed" ? (e.summary ?? null) : null,
        final_status: e.status,
      });
      continue;
    }
    // title and created_at are written by the watcher with the SAME value
    // on every event for a given ticket (frontmatter `created` field, H1
    // line in the body). We don't update them after the first event --
    // doing so risks reordering tickets if a later event happens to carry
    // a different fallback value (e.g. ticket_id when no H1 is present).

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
  }
  return records;
}

/** Group events into turns and attribute tasks per turn. */
export function buildTurns(events: TranscriptEvent[]): Turn[] {
  // Identify turn boundaries by REAL user_message timestamps, in order.
  // Skill expansions, stop-hook feedback, and similar internal pseudo-user
  // events also arrive as user_message events but must not split a turn --
  // see isNonBoundaryUserMessage. They are included in body_events instead
  // so the ChatPanel can still render them inline as collapsible chips.
  const userMessages: TranscriptEvent[] = events
    .filter((e) => e.type === "user_message" && !isNonBoundaryUserMessage(e.content ?? ""))
    .slice()
    .sort((a, b) => a.timestamp.localeCompare(b.timestamp));

  if (userMessages.length === 0) {
    return [];
  }

  const taskRecords = buildTaskRecords(events);

  const turns: Turn[] = [];
  for (let i = 0; i < userMessages.length; i++) {
    const userEvent = userMessages[i];
    const start_ts = userEvent.timestamp;
    const end_ts = i + 1 < userMessages.length ? userMessages[i + 1].timestamp : "";

    const body_events: TranscriptEvent[] = [];
    for (const e of events) {
      if (e === userEvent) continue;
      const isNonBoundaryUser = e.type === "user_message" && isNonBoundaryUserMessage(e.content ?? "");
      if (e.type !== "assistant_message" && e.type !== "tool_result" && !isNonBoundaryUser) continue;
      if (e.timestamp < start_ts) continue;
      if (end_ts !== "" && e.timestamp >= end_ts) continue;
      body_events.push(e);
    }
    body_events.sort((a, b) => a.timestamp.localeCompare(b.timestamp));

    turns.push({
      user_event: userEvent,
      start_ts,
      end_ts,
      body_events,
      tasks: [], // filled below
    });
  }

  // Attribute each task record to its owning turn (created during) and
  // any carryover turn (next turn after the owning one if not closed).
  for (const record of taskRecords.values()) {
    for (let i = 0; i < turns.length; i++) {
      const turn = turns[i];
      const inWindow = record.created_at >= turn.start_ts && (turn.end_ts === "" || record.created_at < turn.end_ts);
      if (!inWindow) continue;
      // Owning turn entry.
      turn.tasks.push(makeTaskInTurn(record, turn, /* is_carryover */ false, turns.length, /* turn_index */ i));

      // Carryover: propagate to every subsequent turn that began before
      // the task closed. Stop as soon as we hit a turn whose start is
      // past the task's closed_at, because every later turn also starts
      // past closed_at. Tasks that never close stay visible in every
      // subsequent turn (closed_at is null -> the predicate is false).
      for (let j = i + 1; j < turns.length; j++) {
        const next = turns[j];
        const closedBeforeNext = record.closed_at !== null && record.closed_at < next.start_ts;
        if (closedBeforeNext) break;
        next.tasks.unshift(makeTaskInTurn(record, next, /* is_carryover */ true, turns.length, /* turn_index */ j));
      }
      break;
    }
  }

  // Within a turn, sort own (non-carryover) tasks by created_at so a
  // still-pending task created later than the in-progress task renders
  // below it (carryovers already at the top from unshift).
  for (const turn of turns) {
    const carry = turn.tasks.filter((t) => t.is_carryover);
    const own = turn.tasks.filter((t) => !t.is_carryover).sort((a, b) => a.created_at.localeCompare(b.created_at));
    turn.tasks = [...carry, ...own];
  }

  return turns;
}

function makeTaskInTurn(
  record: TaskRecord,
  turn: Turn,
  is_carryover: boolean,
  total_turns: number,
  turn_index: number,
): TaskInTurn {
  // Status as of THIS turn's end. Determined by walking the record's
  // transitions: if closed_at is before turn end, status is done; else
  // if started_at is before turn end, status is active; else pending.
  const turnEnd = turn.end_ts;
  let status: TaskUiStatus = "pending";
  if (record.started_at !== null && (turnEnd === "" || record.started_at < turnEnd)) {
    status = "active";
  }
  if (record.closed_at !== null && (turnEnd === "" || record.closed_at < turnEnd)) {
    status = "done";
  }
  // continues_forward: still-open at end of this turn AND another turn
  // exists after this one. The latest turn never qualifies because its
  // tasks are still live (the spinner is honest there).
  const is_last_turn = turn_index === total_turns - 1;
  const continues_forward = status !== "done" && !is_last_turn;
  return {
    ticket_id: record.ticket_id,
    title: record.title,
    status,
    // Summary only renders for done tasks. Carryover entries show summary
    // too if they got closed during this turn.
    summary: status === "done" ? record.summary : null,
    is_carryover,
    continues_forward,
    created_at: record.created_at,
    // Pending tasks have no active window -- they haven't started yet,
    // so they own none of the body events. (Without this guard a pending
    // task's window would default to created_at..end-of-turn and scoop
    // up the in-progress task's tool calls when the user expanded it.)
    active_window_start: status === "pending" ? null : (record.started_at ?? record.created_at),
    active_window_end: status === "done" ? record.closed_at : null,
  };
}

/** Pick out the body_events that fall inside a task's active window.
 *  Used to populate the expanded panel for a given task.
 *
 *  Tool results whose timestamp lands just after `active_window_end` are
 *  pulled in when their `tool_call_id` matches a tool_use already in the
 *  window -- otherwise the expanded panel would render the tool_use as
 *  "unresolved" purely because the result arrived a few ms after the
 *  ticket closed. */
export function eventsInTaskWindow(task: TaskInTurn, body_events: TranscriptEvent[]): TranscriptEvent[] {
  const start = task.active_window_start ?? "";
  const end = task.active_window_end ?? "";
  if (start === "") return [];
  const inWindow = body_events.filter((e) => {
    if (e.timestamp < start) return false;
    if (end !== "" && e.timestamp > end) return false;
    return true;
  });
  if (end === "") return inWindow;
  // Collect tool_call_ids issued by tool_uses that landed in the window
  // so we can pull their (slightly-later) tool_results back in.
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
