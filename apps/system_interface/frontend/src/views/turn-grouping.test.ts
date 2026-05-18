import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import type { TaskInTurn } from "./turn-grouping";
import { buildTaskRecords, buildTurns, eventsInTaskWindow, selectFinalMessages } from "./turn-grouping";

function userMsg(ts: string, content: string, eventId: string = `u-${ts}`): TranscriptEvent {
  return {
    timestamp: ts,
    type: "user_message",
    event_id: eventId,
    source: "test",
    content,
  };
}

function assistantMsg(ts: string, text: string, eventId: string = `a-${ts}`): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: eventId,
    source: "test",
    text,
    tool_calls: [],
  };
}

function toolUse(ts: string, toolName: string, callId: string, input: string = "{}"): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_preview: input }],
  };
}

function taskEvent(
  ticketId: string,
  status: "open" | "in_progress" | "closed",
  ts: string,
  extras: Partial<TranscriptEvent> = {},
): TranscriptEvent {
  return {
    timestamp: ts,
    type: "task_event",
    event_id: `${ticketId}-${status}`,
    source: "tk",
    ticket_id: ticketId,
    title: extras.title ?? "Some task",
    status,
    created_at: extras.created_at ?? ts,
    summary: extras.summary ?? null,
    summary_at: extras.summary_at ?? null,
    step: extras.step,
    parent_id: extras.parent_id,
    assignee: extras.assignee,
  };
}

describe("buildTaskRecords", () => {
  it("folds three events for one ticket into a single record with all timestamps", () => {
    const events = [
      taskEvent("t1", "open", "2026-04-28T01:00:00Z", { created_at: "2026-04-28T01:00:00Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:01:00Z"),
      taskEvent("t1", "closed", "2026-04-28T01:02:00Z", {
        summary: "Did the thing.",
        summary_at: "2026-04-28T01:01:50Z",
      }),
    ];
    const records = buildTaskRecords(events);
    const record = records.get("t1");
    expect(record).toBeDefined();
    expect(record?.created_at).toBe("2026-04-28T01:00:00Z");
    expect(record?.started_at).toBe("2026-04-28T01:01:00Z");
    expect(record?.closed_at).toBe("2026-04-28T01:02:00Z");
    expect(record?.summary).toBe("Did the thing.");
    expect(record?.final_status).toBe("closed");
  });

  it("ignores non-task events", () => {
    const events = [userMsg("2026-04-28T01:00:00Z", "hi"), assistantMsg("2026-04-28T01:00:01Z", "hello")];
    const records = buildTaskRecords(events);
    expect(records.size).toBe(0);
  });

  it("falls back to the event timestamp when created_at is an empty string", () => {
    // Malformed ticket missing the `created:` frontmatter line: the watcher
    // emits an empty created_at. Without the `||` fallback, the resulting
    // TaskRecord has created_at="" and the task gets silently dropped from
    // every turn in buildTurns (empty string fails the window check).
    const events = [taskEvent("t1", "open", "2026-04-28T01:00:00Z", { created_at: "" })];
    const records = buildTaskRecords(events);
    expect(records.get("t1")?.created_at).toBe("2026-04-28T01:00:00Z");
  });
});

describe("buildTurns", () => {
  it("returns no turns when there are no user messages", () => {
    const events = [assistantMsg("2026-04-28T01:00:00Z", "stray reply")];
    expect(buildTurns(events)).toEqual([]);
  });

  it("groups assistant messages and tool_results into the right turn", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "first"),
      assistantMsg("2026-04-28T01:00:30Z", "first reply"),
      userMsg("2026-04-28T01:01:00Z", "second"),
      assistantMsg("2026-04-28T01:01:30Z", "second reply"),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(2);
    expect(turns[0].body_events.map((e) => e.event_id)).toEqual(["a-2026-04-28T01:00:30Z"]);
    expect(turns[1].body_events.map((e) => e.event_id)).toEqual(["a-2026-04-28T01:01:30Z"]);
  });

  it("plain turn (no task_events) has empty tasks array", () => {
    const events = [userMsg("2026-04-28T01:00:00Z", "hi"), assistantMsg("2026-04-28T01:00:01Z", "hello")];
    const turns = buildTurns(events);
    expect(turns[0].tasks).toEqual([]);
  });

  it("attributes a task to the turn it was created in", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "fix the thing"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", {
        created_at: "2026-04-28T01:00:10Z",
        title: "Look at the thing",
      }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:00:50Z", {
        summary: "Found the thing",
        summary_at: "2026-04-28T01:00:45Z",
      }),
      assistantMsg("2026-04-28T01:00:55Z", "Done."),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(1);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[0].tasks[0]).toMatchObject({
      ticket_id: "t1",
      title: "Look at the thing",
      status: "done",
      summary: "Found the thing",
      is_carryover: false,
    });
  });

  it("carries over an unfinished task to the next turn as a fresh entry, leaving the old one frozen", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "fix one"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Step 1" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      // user message arrives while t1 is still in_progress
      userMsg("2026-04-28T01:01:00Z", "any update?"),
      taskEvent("t1", "closed", "2026-04-28T01:01:30Z", {
        summary: "Wrapped up step 1",
        summary_at: "2026-04-28T01:01:25Z",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(2);
    // First turn: ticket appears as "active" (frozen at end of turn 1).
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[0].tasks[0]).toMatchObject({
      ticket_id: "t1",
      status: "active",
      summary: null,
      is_carryover: false,
    });
    // Second turn: same ticket as a CARRYOVER, now "done".
    expect(turns[1].tasks).toHaveLength(1);
    expect(turns[1].tasks[0]).toMatchObject({
      ticket_id: "t1",
      status: "done",
      summary: "Wrapped up step 1",
      is_carryover: true,
    });
  });

  it("does not carry over a task that was closed before the next turn", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "fix one"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Step 1" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:00:50Z", {
        summary: "Done",
        summary_at: "2026-04-28T01:00:45Z",
      }),
      userMsg("2026-04-28T01:01:00Z", "another thing"),
    ];
    const turns = buildTurns(events);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[1].tasks).toHaveLength(0);
  });

  it("carries a still-open task across multiple subsequent turns", () => {
    // Scenario: agent opens a ticket in turn 0, user replies twice
    // (e.g. mid-task permission grant + follow-up) before the task ever
    // closes. The progress block must stay visible in BOTH replies so
    // the user can see the ongoing work, not just raw tool calls.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "start"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Long task" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      userMsg("2026-04-28T01:01:00Z", "grant the permission"),
      userMsg("2026-04-28T01:02:00Z", "still good?"),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(3);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[0].tasks[0]).toMatchObject({ ticket_id: "t1", is_carryover: false, status: "active" });
    expect(turns[1].tasks).toHaveLength(1);
    expect(turns[1].tasks[0]).toMatchObject({ ticket_id: "t1", is_carryover: true, status: "active" });
    expect(turns[2].tasks).toHaveLength(1);
    expect(turns[2].tasks[0]).toMatchObject({ ticket_id: "t1", is_carryover: true, status: "active" });
  });

  it("stops carrying a task forward once it closes", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "start"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Mid task" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      userMsg("2026-04-28T01:01:00Z", "reply 1"),
      taskEvent("t1", "closed", "2026-04-28T01:01:30Z", { summary: "Done.", summary_at: "2026-04-28T01:01:25Z" }),
      userMsg("2026-04-28T01:02:00Z", "reply 2"),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(3);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[1].tasks).toHaveLength(1);
    expect(turns[1].tasks[0]).toMatchObject({ ticket_id: "t1", is_carryover: true, status: "done" });
    // Turn 2 starts AFTER the close: no more carryover.
    expect(turns[2].tasks).toHaveLength(0);
  });

  it("does not split a turn when a skill-expansion user_message arrives mid-turn", () => {
    // Skill expansions arrive as user_message events whose content starts
    // with "Base directory for this skill:". They must NOT be treated as
    // turn boundaries or one logical turn would visibly fracture into
    // many, scattering its tasks across the fragments.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "do the thing"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "First" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:15Z"),
      userMsg(
        "2026-04-28T01:00:20Z",
        "Base directory for this skill: /home/.claude/skills/build-web-service/\n...",
        "skill-1",
      ),
      taskEvent("t2", "open", "2026-04-28T01:00:30Z", { created_at: "2026-04-28T01:00:30Z", title: "Second" }),
      taskEvent("t1", "closed", "2026-04-28T01:00:40Z", { summary: "Did it." }),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(1);
    expect(turns[0].tasks.map((t) => t.title)).toEqual(["First", "Second"]);
    // The skill chip is included in body_events so ChatPanel can render it
    // inline without it acting as a boundary.
    expect(turns[0].body_events.some((e) => e.event_id === "skill-1")).toBe(true);
  });

  it("does not split a turn when a stop-hook-feedback user_message arrives mid-turn", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z" }),
      userMsg("2026-04-28T01:00:20Z", "Stop hook feedback:\n...", "stop-1"),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:25Z"),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(1);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[0].body_events.some((e) => e.event_id === "stop-1")).toBe(true);
  });

  it("gives pending tasks no active window so they own no body events", () => {
    // A pending task that ALSO has an active sibling must not scoop up
    // the sibling's tool calls when expanded. (Before the fix the
    // pending task's window defaulted to created_at..end-of-turn.)
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      taskEvent("active", "open", "2026-04-28T01:00:05Z", { created_at: "2026-04-28T01:00:05Z", title: "Active" }),
      taskEvent("active", "in_progress", "2026-04-28T01:00:10Z"),
      taskEvent("pending", "open", "2026-04-28T01:00:12Z", { created_at: "2026-04-28T01:00:12Z", title: "Pending" }),
      toolUse("2026-04-28T01:00:20Z", "Read", "tc-active"),
    ];
    const turns = buildTurns(events);
    const pendingTask = turns[0].tasks.find((t) => t.ticket_id === "pending");
    expect(pendingTask).toBeDefined();
    expect(pendingTask?.status).toBe("pending");
    expect(pendingTask?.active_window_start).toBeNull();
    expect(eventsInTaskWindow(pendingTask!, turns[0].body_events)).toEqual([]);
  });

  it("orders carryover tasks above own tasks in a turn", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "first"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Carryover" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      userMsg("2026-04-28T01:01:00Z", "second"),
      taskEvent("t2", "open", "2026-04-28T01:01:10Z", { created_at: "2026-04-28T01:01:10Z", title: "Fresh" }),
    ];
    const turns = buildTurns(events);
    expect(turns[1].tasks.map((t) => t.title)).toEqual(["Carryover", "Fresh"]);
    expect(turns[1].tasks[0].is_carryover).toBe(true);
    expect(turns[1].tasks[1].is_carryover).toBe(false);
  });

  it("orders own tasks by started_at, not created_at, when the agent starts them out of order", () => {
    // Agent plans two tickets up-front (t1 then t2), then starts t2
    // FIRST and t1 SECOND. The end-of-turn order must reflect what the
    // agent actually did (t2 above t1), not the order they were planned.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "do it"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "First planned" }),
      taskEvent("t2", "open", "2026-04-28T01:00:20Z", { created_at: "2026-04-28T01:00:20Z", title: "Second planned" }),
      taskEvent("t2", "in_progress", "2026-04-28T01:00:30Z"),
      taskEvent("t2", "closed", "2026-04-28T01:00:40Z", { summary: "Did t2 first." }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:50Z"),
    ];
    const turns = buildTurns(events);
    expect(turns[0].tasks.map((t) => t.title)).toEqual(["Second planned", "First planned"]);
  });

  it("sorts not-yet-started tasks by created_at after started ones", () => {
    // t1 and t2 are planned and t1 is started; t3 is planned later and
    // never started this turn. Started t1 sorts by its started_at;
    // pending t2 and t3 fall back to created_at.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Alpha" }),
      taskEvent("t2", "open", "2026-04-28T01:00:20Z", { created_at: "2026-04-28T01:00:20Z", title: "Bravo" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:25Z"),
      taskEvent("t3", "open", "2026-04-28T01:00:30Z", { created_at: "2026-04-28T01:00:30Z", title: "Charlie" }),
    ];
    const turns = buildTurns(events);
    expect(turns[0].tasks.map((t) => t.title)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect(turns[0].tasks.map((t) => t.status)).toEqual(["active", "pending", "pending"]);
  });

  it("orders multiple carryover tasks by started_at, not by reverse insertion", () => {
    // Repro of the chat-progress bug: a clarifying-question turn plans
    // two tickets up-front; the next turn starts the FIRST one. Both are
    // carryovers in the second turn, and the earlier-created (active) one
    // must render above the later-created (still pending) one.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "plan it"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Pull emails" }),
      taskEvent("t2", "open", "2026-04-28T01:00:20Z", { created_at: "2026-04-28T01:00:20Z", title: "Sort sample" }),
      userMsg("2026-04-28T01:01:00Z", "go"),
      taskEvent("t1", "in_progress", "2026-04-28T01:01:05Z"),
    ];
    const turns = buildTurns(events);
    expect(turns[1].tasks.map((t) => t.title)).toEqual(["Pull emails", "Sort sample"]);
    expect(turns[1].tasks.map((t) => t.status)).toEqual(["active", "pending"]);
    expect(turns[1].tasks.every((t) => t.is_carryover)).toBe(true);
  });
});

describe("eventsInTaskWindow", () => {
  it("returns only events between a task's started_at and closed_at", () => {
    const tasksByTime = {
      ticket_id: "t1",
      title: "Step 1",
      status: "done" as const,
      summary: "Did it",
      is_carryover: false,
      continues_forward: false,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:20Z",
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: "2026-04-28T01:00:50Z",
      is_step: false,
      parent_id: "",
      children: [],
      narration: null,
    };
    const body = [
      assistantMsg("2026-04-28T01:00:15Z", "before start"),
      toolUse("2026-04-28T01:00:25Z", "Read", "tc1"),
      toolUse("2026-04-28T01:00:45Z", "Edit", "tc2"),
      assistantMsg("2026-04-28T01:00:55Z", "after end"),
    ];
    const result = eventsInTaskWindow(tasksByTime, body);
    expect(result.map((e) => e.event_id)).toEqual(["a-tc1", "a-tc2"]);
  });

  it("returns events through end of turn when active_window_end is null", () => {
    const task = {
      ticket_id: "t1",
      title: "Active",
      status: "active" as const,
      summary: null,
      is_carryover: false,
      continues_forward: false,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:20Z",
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: null,
      is_step: false,
      parent_id: "",
      children: [],
      narration: null,
    };
    const body = [toolUse("2026-04-28T01:00:25Z", "Read", "tc1"), toolUse("2026-04-28T01:00:45Z", "Edit", "tc2")];
    expect(eventsInTaskWindow(task, body)).toHaveLength(2);
  });

  it("pulls in a trailing tool_result whose tool_use was in window", () => {
    // The tool_use lands inside the window; its tool_result arrives a few
    // ms after closed_at. Without the trailing-result fallback the
    // expanded panel would render the tool call as unresolved.
    const task = {
      ticket_id: "t1",
      title: "Step 1",
      status: "done" as const,
      summary: "Did it",
      is_carryover: false,
      continues_forward: false,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:20Z",
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: "2026-04-28T01:00:50Z",
      is_step: false,
      parent_id: "",
      children: [],
      narration: null,
    };
    function toolResult(ts: string, callId: string): TranscriptEvent {
      return {
        timestamp: ts,
        type: "tool_result",
        event_id: `r-${callId}`,
        source: "test",
        tool_call_id: callId,
        output: "ok",
      };
    }
    const body = [
      toolUse("2026-04-28T01:00:45Z", "Read", "tc-late"),
      // Result timestamp is 1 second past closed_at -- still belongs to
      // this task's expanded panel.
      toolResult("2026-04-28T01:00:51Z", "tc-late"),
    ];
    const result = eventsInTaskWindow(task, body);
    expect(result.map((e) => e.event_id)).toEqual(["a-tc-late", "r-tc-late"]);
  });
});

describe("selectFinalMessages", () => {
  it("returns every text-only assistant_message in chronological order", () => {
    // Regression: the previous "last non-empty assistant_message" heuristic
    // dropped earlier prose. With multiple separate text-only messages in
    // a single turn (e.g. a summary table followed by a "waiting on your
    // input" line) the user only saw the second one. Now both must come
    // back, in arrival order.
    const a1 = assistantMsg("2026-04-28T01:00:10Z", "Here is the summary table...", "msg-summary");
    const a2 = assistantMsg("2026-04-28T01:00:20Z", "Waiting on your input.", "msg-waiting");
    const events = [a1, toolUse("2026-04-28T01:00:15Z", "Bash", "tc-1"), a2];
    expect(selectFinalMessages(events, []).map((e) => e.event_id)).toEqual(["msg-summary", "msg-waiting"]);
  });

  it("excludes tool-bearing assistant_messages even when they have text", () => {
    // Tool-bearing messages live inside the task's expanded panel where
    // their tool_calls render. Pulling them up would orphan the tool
    // calls.
    const withTextAndTools: TranscriptEvent = {
      timestamp: "2026-04-28T01:00:00Z",
      type: "assistant_message",
      event_id: "a-mixed",
      source: "test",
      text: "Calling out to a tool.",
      tool_calls: [{ tool_call_id: "tc-x", tool_name: "Bash", input_preview: "{}" }],
    };
    expect(selectFinalMessages([withTextAndTools], [])).toEqual([]);
  });

  it("excludes empty-text assistant_messages", () => {
    // Streaming/partial messages and pure tool_use events both serialize
    // with text="". They aren't substantive prose and should not surface
    // as top-level final blocks.
    const empty = assistantMsg("2026-04-28T01:00:00Z", "", "a-empty");
    expect(selectFinalMessages([empty], [])).toEqual([]);
  });

  it("ignores non-assistant events", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "hello"),
      taskEvent("t1", "open", "2026-04-28T01:00:01Z"),
      assistantMsg("2026-04-28T01:00:02Z", "hi back", "a-hi"),
    ];
    expect(selectFinalMessages(events, []).map((e) => e.event_id)).toEqual(["a-hi"]);
  });

  it("drops a text-only message that falls inside a closed task's window", () => {
    // Mid-task prose belongs to the closed task's narration slot (which
    // the close summary then overrides). Surfacing it at top level
    // duplicates the agent's output and clutters the final view.
    const doneTask: TaskInTurn = {
      ticket_id: "t1",
      title: "Pull headlines",
      status: "done",
      summary: "Pulled headline + summary + link from each newsletter.",
      is_carryover: false,
      continues_forward: false,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:01:00Z",
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
    };
    const mid = assistantMsg("2026-04-28T01:00:30Z", "Half done with the JSON.", "msg-mid");
    expect(selectFinalMessages([mid], [doneTask])).toEqual([]);
  });

  it("keeps a text-only message that falls outside every task window", () => {
    // Prose between tasks (after one task closed, before the next
    // started) has no task to attach to -- it must render at top level
    // or the user would never see it.
    const earlier: TaskInTurn = {
      ticket_id: "t1",
      title: "Setup",
      status: "done",
      summary: "Set things up.",
      is_carryover: false,
      continues_forward: false,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:30Z",
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
    };
    const between = assistantMsg("2026-04-28T01:00:40Z", "Quick check-in before next step.", "msg-between");
    expect(selectFinalMessages([between], [earlier]).map((e) => e.event_id)).toEqual(["msg-between"]);
  });

  it("surfaces the trailing message of an unclosed task at top level (safety valve)", () => {
    // If the agent leaves the task open at turn end and emits a final
    // text message, that message would otherwise sit only in the
    // narration slot of an unresolved task. Promote it to top level so
    // the user reliably sees the wrap-up.
    const openTask: TaskInTurn = {
      ticket_id: "t1",
      title: "Investigate",
      status: "active",
      summary: null,
      is_carryover: false,
      continues_forward: false,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
    };
    const trailing = assistantMsg("2026-04-28T01:00:50Z", "Stuck -- need your call.", "msg-trailing");
    expect(selectFinalMessages([trailing], [openTask]).map((e) => e.event_id)).toEqual(["msg-trailing"]);
  });

  it("drops earlier in-window messages even when the last one is the trailing safety-valve case", () => {
    // The safety valve only rescues the LAST text-only message of the
    // turn. Earlier in-window prose still belongs to the narration slot.
    const openTask: TaskInTurn = {
      ticket_id: "t1",
      title: "Investigate",
      status: "active",
      summary: null,
      is_carryover: false,
      continues_forward: false,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
    };
    const mid = assistantMsg("2026-04-28T01:00:20Z", "Looking into thing A.", "msg-mid");
    const trailing = assistantMsg("2026-04-28T01:00:50Z", "Hit a wall, need your call.", "msg-trailing");
    expect(selectFinalMessages([mid, trailing], [openTask]).map((e) => e.event_id)).toEqual(["msg-trailing"]);
  });
});

describe("narration attribution", () => {
  it("populates narration with the latest text-only message inside an active task's window", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Do the thing", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
      assistantMsg("2026-04-28T01:00:10Z", "Trying approach A.", "a1"),
      assistantMsg("2026-04-28T01:00:20Z", "Approach A failed, trying B.", "a2"),
    ];
    const [turn] = buildTurns(events);
    expect(turn.tasks).toHaveLength(1);
    expect(turn.tasks[0].narration).toBe("Approach A failed, trying B.");
  });

  it("leaves narration null on a closed task -- the summary owns the slot", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Do the thing", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
      assistantMsg("2026-04-28T01:00:10Z", "Mid-task narration.", "a1"),
      taskEvent("t1", "closed", "2026-04-28T01:00:30Z", { summary: "Did the thing.", step: true }),
    ];
    const [turn] = buildTurns(events);
    expect(turn.tasks[0].status).toBe("done");
    expect(turn.tasks[0].summary).toBe("Did the thing.");
    expect(turn.tasks[0].narration).toBeNull();
  });
});

describe("step + ticket nesting", () => {
  it("nests step children under their parent ticket in the same turn", () => {
    // Agent picks up a ticket and files two step records under it within
    // the same turn. The progress block should show the ticket as a
    // top-level node with both steps in its `children`.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "do the auth refactor"),
      taskEvent("auth-1", "in_progress", "2026-04-28T01:00:05Z", {
        title: "Refactor auth middleware",
        assignee: "agent-A",
      }),
      taskEvent("step-1", "open", "2026-04-28T01:00:10Z", {
        title: "Read the middleware",
        step: true,
        parent_id: "auth-1",
      }),
      taskEvent("step-2", "open", "2026-04-28T01:00:20Z", {
        title: "Edit it",
        step: true,
        parent_id: "auth-1",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(1);
    const tasks = turns[0].tasks;
    // After nesting only the parent ticket survives as a top-level task.
    expect(tasks.map((t) => t.ticket_id)).toEqual(["auth-1"]);
    expect(tasks[0].is_step).toBe(false);
    expect(tasks[0].children.map((c) => c.ticket_id)).toEqual(["step-1", "step-2"]);
    expect(tasks[0].children.every((c) => c.is_step)).toBe(true);
  });

  it("leaves a standalone step (no parent) at the top level", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "quick lookup"),
      taskEvent("step-x", "open", "2026-04-28T01:00:05Z", {
        title: "Read the README",
        step: true,
        parent_id: "",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[0].tasks[0].is_step).toBe(true);
    expect(turns[0].tasks[0].children).toEqual([]);
  });

  it("orphan step (parent absent from turn) renders flat at the top level", () => {
    // The step's parent_id points at a ticket that isn't in this turn's
    // task list (e.g. it closed in an earlier turn). The renderer falls
    // back to flat placement rather than dropping the step.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "follow up"),
      taskEvent("step-orphan", "open", "2026-04-28T01:00:05Z", {
        title: "Stray follow-up",
        step: true,
        parent_id: "ghost-parent",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns[0].tasks.map((t) => t.ticket_id)).toEqual(["step-orphan"]);
    expect(turns[0].tasks[0].children).toEqual([]);
  });
});

describe("picked-up-ticket attribution", () => {
  it("attributes a ticket to the turn containing its earliest observed event, not its created_at", () => {
    // The ticket was originally created long before the current agent
    // saw it (e.g. by agent-A). Agent-B's watcher only starts emitting
    // events once B becomes the assignee, so the earliest event in B's
    // stream is the in_progress one. That timestamp -- not created_at --
    // is what should attribute the ticket to a turn.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T02:00:00Z", "turn 1 prompt"),
      userMsg("2026-04-28T03:00:00Z", "pick up the auth ticket"),
      // The ticket frontmatter says it was created on day 1, well before
      // either turn began. But B's watcher only saw it transition to
      // in_progress (with assignee=B) at 03:00:30, inside turn 2.
      taskEvent("auth-1", "in_progress", "2026-04-28T03:00:30Z", {
        title: "Auth refactor",
        assignee: "agent-B",
        created_at: "2026-04-27T10:00:00Z",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(2);
    // The ticket lands in the picker's turn (turn 2), not the originator's.
    expect(turns[0].tasks).toEqual([]);
    expect(turns[1].tasks.map((t) => t.ticket_id)).toEqual(["auth-1"]);
  });

  it("falls back to created_at when the record has no first_observed_at signal", () => {
    // Sanity: regular own-ticket flow still works. The agent created the
    // ticket in this turn, so first_observed_at == created_at and the
    // attribution behaves identically to the legacy rule.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "plan it"),
      taskEvent("own-1", "open", "2026-04-28T01:00:05Z", {
        title: "Plan step",
        step: true,
        created_at: "2026-04-28T01:00:05Z",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns[0].tasks.map((t) => t.ticket_id)).toEqual(["own-1"]);
  });
});

describe("parent + children carryover", () => {
  it("carries the parent ticket forward and nests new T2 child steps under it", () => {
    // Plan scenario: B picks up auth-1 in T1, files step-1, closes it.
    // In T2 B files step-2 under the same (still-in_progress) ticket.
    // T2's progress block re-renders the parent + the newly added step.
    // The closed step from T1 does NOT re-render in T2 -- standard
    // carryover only propagates tasks that are still unfinished.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "do the refactor"),
      taskEvent("auth-1", "in_progress", "2026-04-28T01:00:05Z", {
        title: "Auth refactor",
        assignee: "agent-A",
      }),
      taskEvent("step-1", "open", "2026-04-28T01:00:10Z", {
        title: "Read it",
        step: true,
        parent_id: "auth-1",
      }),
      taskEvent("step-1", "closed", "2026-04-28T01:00:30Z", {
        title: "Read it",
        step: true,
        parent_id: "auth-1",
        summary: "Read the middleware end-to-end.",
        summary_at: "2026-04-28T01:00:30Z",
      }),
      userMsg("2026-04-28T02:00:00Z", "continue"),
      taskEvent("step-2", "open", "2026-04-28T02:00:05Z", {
        title: "Patch it",
        step: true,
        parent_id: "auth-1",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(2);
    // Turn 1: parent with step-1 nested.
    expect(turns[0].tasks.map((t) => t.ticket_id)).toEqual(["auth-1"]);
    expect(turns[0].tasks[0].children.map((c) => c.ticket_id)).toEqual(["step-1"]);
    expect(turns[0].tasks[0].continues_forward).toBe(true);
    // Turn 2: parent carries over, step-2 nests under it. step-1 is
    // already closed and does NOT re-render.
    expect(turns[1].tasks.map((t) => t.ticket_id)).toEqual(["auth-1"]);
    expect(turns[1].tasks[0].children.map((c) => c.ticket_id)).toEqual(["step-2"]);
  });
});
