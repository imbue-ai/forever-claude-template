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
 *   1. Fold task_event events by ticket_id into TaskRecord (the latest
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
  /** True iff the underlying tk file has `step: true` (turn-bound
   *  progress marker). Step records nest under their parent ticket in
   *  the progress view; standalone steps render flat. */
  step: boolean;
  /** Id of the parent ticket this record is nested under, or "". */
  parent_id: string;
  /** Current assignee from the latest event (used for diagnostics; the
   *  watcher already filters which agent sees the ticket, so the
   *  frontend doesn't re-check this for routing). */
  assignee: string;
  /** The earliest task_event timestamp observed for this ticket in the
   *  current agent's stream. Used for turn attribution instead of
   *  `created_at` so a ticket picked up by THIS agent lands in the
   *  picker's first-action turn rather than the originator's creation
   *  turn (whose timestamp could be in any prior turn -- or any prior
   *  agent's runtime -- when a ticket is handed off). */
  first_observed_at: string;
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
  /** Timestamp the ticket was created. Fallback sort key for tasks that
   *  haven't started yet by the end of THIS turn. */
  created_at: string;
  /** Timestamp the ticket transitioned to in_progress, but only if that
   *  transition happened by the end of THIS turn. Null for tasks still
   *  pending in this turn. Used as the primary sort key so tasks within
   *  a turn render in the order the agent actually started them, not the
   *  order they were planned/created. */
  started_at: string | null;
  /** Inclusive lower bound of the active window for tool-call attribution. */
  active_window_start: string | null;
  /** Inclusive upper bound of the active window. null = still active at
   *  end of turn. */
  active_window_end: string | null;
  /** True iff this node represents a step record (turn-bound progress
   *  marker) rather than a regular ticket. Drives ProgressBlock chrome:
   *  steps render slimmer and (when nested) indented under their
   *  parent; regular tickets render with a parent badge / heavier
   *  border. */
  is_step: boolean;
  /** Id of the parent ticket this node is nested under, or "" if
   *  standalone. Used during the nesting pass in buildTurns to fold a
   *  step into its parent's `children` when both live in the same
   *  turn. */
  parent_id: string;
  /** Step children grouped under this node (regular ticket or
   *  standalone step). Empty when this node has no children in the
   *  current turn. The renderer walks this recursively but in practice
   *  the model is two levels: ticket -> steps, with steps having no
   *  children of their own. */
  children: TaskInTurn[];
  /** Live status caption rendered under the task title, always visible.
   *  Holds the text of the latest text-only assistant_message that fell
   *  inside this task's active window. While the task is active this
   *  acts as a "what's happening now" caption; once the task closes the
   *  ProgressBlock renders `summary` instead (or nothing, if there's no
   *  summary -- final state stays clean). Null when no text-only
   *  message has landed in the window yet. */
  narration: string | null;
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
        step: e.step ?? false,
        parent_id: e.parent_id ?? "",
        assignee: e.assignee ?? "",
        first_observed_at: e.timestamp,
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
    // Track the earliest event timestamp seen in THIS agent's stream.
    // For a regular ticket picked up from another agent, the first
    // event in our stream is an in_progress one (the watcher only
    // started surfacing it once we became assignee) -- earlier than
    // any subsequent close, so the min is the picker's first action.
    if (e.timestamp < existing.first_observed_at) {
      existing.first_observed_at = e.timestamp;
    }
    // assignee changes over the ticket's life; the latest observed
    // value wins. step / parent_id are immutable post-create in
    // practice (tk doesn't expose a re-parent or convert-to-step
    // operation), but we still update for symmetry / future-proofing.
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

  // Attribute each task record to its owning turn (the first turn whose
  // window contains the earliest task_event we observed for this ticket
  // in the current agent's stream) and any carryover turn. Using
  // `first_observed_at` instead of `created_at` is what makes a ticket
  // PICKED UP by this agent land in the picker's turn rather than the
  // originator's creation turn -- the watcher only starts surfacing
  // events for the ticket once we become its assignee, so its earliest
  // event in our stream IS the picker's first action.
  for (const record of taskRecords.values()) {
    const attribution_ts = record.first_observed_at || record.created_at;
    for (let i = 0; i < turns.length; i++) {
      const turn = turns[i];
      const inWindow = attribution_ts >= turn.start_ts && (turn.end_ts === "" || attribution_ts < turn.end_ts);
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

  // Within a turn, carryovers sit above own tasks; within each group, sort
  // by the task's started_at (when the agent transitioned it to
  // in_progress) so the order at end-of-turn matches the order the agent
  // actually started tasks rather than the order they were planned. Tasks
  // not yet started in this turn sink to the bottom of the group and sort
  // among themselves by created_at. So e.g. if the agent plans t1 then t2
  // up-front but starts t2 first, t2 renders above t1; a still-pending t3
  // created after both renders below both.
  const byStart = (a: TaskInTurn, b: TaskInTurn) => {
    if (a.started_at !== null && b.started_at !== null) return a.started_at.localeCompare(b.started_at);
    if (a.started_at !== null) return -1;
    if (b.started_at !== null) return 1;
    return a.created_at.localeCompare(b.created_at);
  };
  for (const turn of turns) {
    const carry = turn.tasks.filter((t) => t.is_carryover).sort(byStart);
    const own = turn.tasks.filter((t) => !t.is_carryover).sort(byStart);
    turn.tasks = [...carry, ...own];
  }

  // Per-task narration: attribute each text-only assistant_message in the
  // turn to the most-recently-started task whose active window contains
  // it, and set that task's `narration` to the message text. Later
  // matches overwrite earlier ones so the slot reflects the LATEST text
  // in window. We skip done tasks because their slot will render
  // `summary` (or nothing) instead -- narration is only meaningful while
  // a task is still in flight. We also skip continues_forward tasks:
  // their last text-only message is promoted to a top-level final
  // message by selectFinalMessages, so populating narration would
  // duplicate it.
  for (const turn of turns) {
    for (const e of turn.body_events) {
      if (e.type !== "assistant_message") continue;
      if (!e.text) continue;
      if (e.tool_calls && e.tool_calls.length > 0) continue;
      const containing = findContainingTask(e.timestamp, turn.tasks);
      if (containing === null) continue;
      if (containing.status === "done") continue;
      if (containing.continues_forward) continue;
      containing.narration = e.text;
    }
  }

  // Drop regular tickets from the rendered timeline. The progress view
  // shows STEPS only -- regular tickets are substantive cross-agent
  // units that can stay open across many turns, and rendering them
  // inline among per-turn step records confuses the per-turn timeline
  // (their windows span multiple turns, their captions don't map onto
  // the "live status" model, and their presence visually clutters what
  // the user reads as "what happened this turn"). Tickets are still
  // tracked in tk for cross-agent coordination; they just don't render
  // in the chat progress block.
  //
  // Any step whose `parent_id` pointed at a regular ticket simply
  // renders flat at top level -- the parent isn't on screen to nest
  // under. (The earlier nesting pass that folded step children into a
  // parent ticket was removed along with the ticket rendering itself.)
  for (const turn of turns) {
    turn.tasks = turn.tasks.filter((t) => t.is_step);
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
  // Turn-local started_at: only expose it as a sort key if the start
  // actually happened by the end of THIS turn. A task whose record has a
  // started_at in a FUTURE turn must still be treated as pending here, so
  // its turn-local started_at is null and it sorts by created_at.
  const started_at_in_turn =
    record.started_at !== null && (turnEnd === "" || record.started_at < turnEnd) ? record.started_at : null;
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
    started_at: started_at_in_turn,
    // Pending tasks have no active window -- they haven't started yet,
    // so they own none of the body events. (Without this guard a pending
    // task's window would default to created_at..end-of-turn and scoop
    // up the in-progress task's tool calls when the user expanded it.)
    active_window_start: status === "pending" ? null : (record.started_at ?? record.created_at),
    active_window_end: status === "done" ? record.closed_at : null,
    is_step: record.step,
    parent_id: record.parent_id,
    children: [],
    narration: null,
  };
}

/** Find the step record whose active window contains `ts`. Returns null
 *  if no step contains the timestamp. Pending steps (no active window)
 *  are skipped.
 *
 *  Regular tickets are intentionally NOT eligible containers. A regular
 *  ticket can stay open across many turns and contain many step records
 *  beneath it; once the latest step closes, any subsequent text-only
 *  message is a wrap-up bounded by the latest step, not by the
 *  outer ticket -- treating the ticket as a container would swallow the
 *  wrap-up into a slot the user can't easily see. Step records are
 *  shorter-lived and are the natural unit for "live status" captions.
 *
 *  Effective windows: CLAUDE.md's step lifecycle says only one step is
 *  in_progress at a time. If the agent forgets to close a step before
 *  starting the next, the stored window of the abandoned step stretches
 *  indefinitely and would otherwise swallow every subsequent message.
 *  We enforce the serial-step invariant in the renderer by capping each
 *  step's effective end at the start of the next step in the turn (when
 *  one exists), so an abandoned step's window ends as soon as a later
 *  step begins -- abandoned steps don't pollute the rest of the turn.
 *
 *  Walks nested children so callers can pass either the flat
 *  pre-nesting list or the post-nesting tree. */
function findContainingTask(ts: string, tasks: TaskInTurn[]): TaskInTurn | null {
  const steps: TaskInTurn[] = [];
  const visit = (t: TaskInTurn): void => {
    if (t.is_step && t.active_window_start !== null) steps.push(t);
    for (const child of t.children) visit(child);
  };
  for (const t of tasks) visit(t);
  steps.sort((a, b) => (a.active_window_start ?? "").localeCompare(b.active_window_start ?? ""));
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    const start = step.active_window_start!;
    if (ts < start) continue;
    const originalEnd = step.active_window_end;
    const nextStart = i + 1 < steps.length ? steps[i + 1].active_window_start : null;
    let effectiveEnd: string | null = originalEnd;
    if (nextStart !== null && (effectiveEnd === null || nextStart < effectiveEnd)) {
      effectiveEnd = nextStart;
    }
    if (effectiveEnd !== null && ts > effectiveEnd) continue;
    return step;
  }
  return null;
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

/**
 * Pick the text-only assistant_messages from a turn that should render
 * at the top level of the progress block (below the timeline), rather
 * than being absorbed into a task's narration slot.
 *
 * Rules:
 *   - Message must be a text-only assistant_message (non-empty `text`
 *     and no `tool_calls`). Tool-bearing messages always stay inside
 *     their task's expanded panel.
 *   - Messages whose timestamp falls outside every task's active
 *     window render at top level (standalone prose between or around
 *     tasks).
 *   - Promote-on-close: the LAST text-only message of the turn, when
 *     its containing task closed within the turn, ALSO renders at top
 *     level. The close summary takes the task's caption slot, so the
 *     agent's last in-flight prose would otherwise vanish; promoting
 *     it preserves the wrap-up where the agent naturally wrote it.
 *     We only fire this for the LAST text-only of the turn, not every
 *     closed task, so mid-turn closures don't keep flushing earlier
 *     intermediate thinking to top level -- only the genuine
 *     end-of-turn wrap-up surfaces.
 *
 *   Promote-on-freeze: the LAST text-only message of the turn, when
 *   its containing task has `continues_forward` set (the turn ended
 *   with the task still open), ALSO renders at top level. The
 *   narration slot on a frozen task is a muted caption that the user
 *   is unlikely to notice; promoting the message makes the agent's
 *   final prose visible as a full reply. The transient-firing concern
 *   does not apply here: `continues_forward` is only true for past
 *   turns (the live turn never sets it), so the message set is
 *   frozen. The narration attribution pass separately skips
 *   continues_forward tasks so there is no duplication.
 */
export function selectFinalMessages(body_events: TranscriptEvent[], tasks: TaskInTurn[]): TranscriptEvent[] {
  const textOnly = body_events.filter(
    (ev) => ev.type === "assistant_message" && !!ev.text && !(ev.tool_calls && ev.tool_calls.length > 0),
  );
  if (textOnly.length === 0) return [];
  const result: TranscriptEvent[] = [];
  for (const ev of textOnly) {
    if (findContainingTask(ev.timestamp, tasks) === null) {
      result.push(ev);
    }
  }
  const lastMsg = textOnly[textOnly.length - 1];
  const lastContaining = findContainingTask(lastMsg.timestamp, tasks);
  if (
    lastContaining !== null &&
    (lastContaining.status === "done" || lastContaining.continues_forward) &&
    !result.includes(lastMsg)
  ) {
    result.push(lastMsg);
  }
  result.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  return result;
}
