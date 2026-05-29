import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import type { StepView } from "./turn-grouping";
import {
  buildTaskRecords,
  makeStepView,
  stepActiveInWindow,
  sortSteps,
  attributeNarration,
  eventsInTaskWindow,
  selectFinalMessages,
} from "./turn-grouping";

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
    step: extras.step ?? true,
    parent_id: extras.parent_id,
    assignee: extras.assignee,
  };
}

/** Helper: build steps active in a window from events, mirroring what
 *  ChatPanel does inline. */
function stepsForWindow(events: TranscriptEvent[], start_ts: string, end_ts: string, is_settled: boolean): StepView[] {
  const records = buildTaskRecords(events);
  const steps: StepView[] = [];
  for (const r of records.values()) {
    if (!r.step) continue;
    if (!stepActiveInWindow(r, start_ts, end_ts)) continue;
    steps.push(makeStepView(r, is_settled && r.final_status !== "closed"));
  }
  return sortSteps(steps, records, start_ts);
}

/** Helper: collect body events in a window from the full event list. */
function bodyEventsInWindow(events: TranscriptEvent[], start_ts: string, end_ts: string): TranscriptEvent[] {
  return events
    .filter((e) => {
      if (e.type !== "assistant_message" && e.type !== "tool_result") return false;
      if (e.timestamp < start_ts) return false;
      if (end_ts !== "" && e.timestamp >= end_ts) return false;
      return true;
    })
    .sort((a, b) => a.timestamp.localeCompare(b.timestamp));
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
    const events = [taskEvent("t1", "open", "2026-04-28T01:00:00Z", { created_at: "" })];
    const records = buildTaskRecords(events);
    expect(records.get("t1")?.created_at).toBe("2026-04-28T01:00:00Z");
  });
});

describe("stepActiveInWindow", () => {
  it("includes a step first observed inside the window", () => {
    const events = [taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z" })];
    const record = buildTaskRecords(events).get("t1")!;
    expect(stepActiveInWindow(record, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z")).toBe(true);
  });

  it("includes a step that started before the window and is still open", () => {
    const events = [
      taskEvent("t1", "open", "2026-04-28T00:50:00Z", { created_at: "2026-04-28T00:50:00Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T00:55:00Z"),
    ];
    const record = buildTaskRecords(events).get("t1")!;
    expect(stepActiveInWindow(record, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z")).toBe(true);
  });

  it("excludes a step that closed before the window started", () => {
    const events = [
      taskEvent("t1", "open", "2026-04-28T00:50:00Z", { created_at: "2026-04-28T00:50:00Z" }),
      taskEvent("t1", "closed", "2026-04-28T00:55:00Z"),
    ];
    const record = buildTaskRecords(events).get("t1")!;
    expect(stepActiveInWindow(record, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z")).toBe(false);
  });

  it("includes a step that started before and closed during the window", () => {
    const events = [
      taskEvent("t1", "open", "2026-04-28T00:50:00Z", { created_at: "2026-04-28T00:50:00Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T00:55:00Z"),
      taskEvent("t1", "closed", "2026-04-28T01:00:30Z"),
    ];
    const record = buildTaskRecords(events).get("t1")!;
    expect(stepActiveInWindow(record, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z")).toBe(true);
  });

  it("works with open-ended window (tail partition)", () => {
    const events = [taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z" })];
    const record = buildTaskRecords(events).get("t1")!;
    expect(stepActiveInWindow(record, "2026-04-28T01:00:00Z", "")).toBe(true);
  });
});

describe("step rendering in partitions", () => {
  it("attributes a step to the partition it was created in", () => {
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
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({
      ticket_id: "t1",
      title: "Look at the thing",
      status: "done",
      summary: "Found the thing",
    });
  });

  it("shows an unfinished step in both partitions when it spans a user message", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Step 1" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:01:30Z", {
        summary: "Wrapped up step 1",
        summary_at: "2026-04-28T01:01:25Z",
      }),
    ];
    // First partition: 01:00:00 -> 01:01:00
    const steps1 = stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z", true);
    expect(steps1).toHaveLength(1);
    expect(steps1[0]).toMatchObject({ ticket_id: "t1", status: "done" });

    // Second partition: 01:01:00 -> "" (tail)
    const steps2 = stepsForWindow(events, "2026-04-28T01:01:00Z", "", false);
    expect(steps2).toHaveLength(1);
    expect(steps2[0]).toMatchObject({ ticket_id: "t1", status: "done" });
  });

  it("does not show a step that was closed before the partition", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Step 1" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:00:50Z"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:01:00Z", "", false);
    expect(steps).toHaveLength(0);
  });

  it("carries a still-open step across multiple partitions", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Long task" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
    ];
    // Partition 1: 01:00 -> 01:01
    expect(stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z", true)).toHaveLength(1);
    // Partition 2: 01:01 -> 01:02
    expect(stepsForWindow(events, "2026-04-28T01:01:00Z", "2026-04-28T01:02:00Z", true)).toHaveLength(1);
    // Partition 3: 01:02 -> tail
    expect(stepsForWindow(events, "2026-04-28T01:02:00Z", "", false)).toHaveLength(1);
  });

  it("gives pending tasks no active window so they own no body events", () => {
    const events: TranscriptEvent[] = [
      taskEvent("active", "open", "2026-04-28T01:00:05Z", { created_at: "2026-04-28T01:00:05Z", title: "Active" }),
      taskEvent("active", "in_progress", "2026-04-28T01:00:10Z"),
      taskEvent("pending", "open", "2026-04-28T01:00:12Z", { created_at: "2026-04-28T01:00:12Z", title: "Pending" }),
      toolUse("2026-04-28T01:00:20Z", "Read", "tc-active"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    const pendingStep = steps.find((s) => s.ticket_id === "pending");
    expect(pendingStep).toBeDefined();
    expect(pendingStep?.status).toBe("pending");
    expect(pendingStep?.active_window_start).toBeNull();
    const body = bodyEventsInWindow(events, "2026-04-28T01:00:00Z", "");
    expect(eventsInTaskWindow(pendingStep!, body)).toEqual([]);
  });

  it("orders carryover steps above own steps", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Carryover" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t2", "open", "2026-04-28T01:01:10Z", { created_at: "2026-04-28T01:01:10Z", title: "Fresh" }),
    ];
    // Second partition: 01:01 -> tail. t1 is carryover, t2 is new.
    const steps = stepsForWindow(events, "2026-04-28T01:01:00Z", "", false);
    expect(steps.map((s) => s.title)).toEqual(["Carryover", "Fresh"]);
  });

  it("orders own steps by started_at, not created_at, when started out of order", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "First planned" }),
      taskEvent("t2", "open", "2026-04-28T01:00:20Z", { created_at: "2026-04-28T01:00:20Z", title: "Second planned" }),
      taskEvent("t2", "in_progress", "2026-04-28T01:00:30Z"),
      taskEvent("t2", "closed", "2026-04-28T01:00:40Z", { summary: "Did t2 first." }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:50Z"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    expect(steps.map((s) => s.title)).toEqual(["Second planned", "First planned"]);
  });

  it("sorts not-yet-started steps by created_at after started ones", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Alpha" }),
      taskEvent("t2", "open", "2026-04-28T01:00:20Z", { created_at: "2026-04-28T01:00:20Z", title: "Bravo" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:25Z"),
      taskEvent("t3", "open", "2026-04-28T01:00:30Z", { created_at: "2026-04-28T01:00:30Z", title: "Charlie" }),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    expect(steps.map((s) => s.title)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect(steps.map((s) => s.status)).toEqual(["active", "pending", "pending"]);
  });

  it("drops regular tickets -- only step records appear", () => {
    const events: TranscriptEvent[] = [
      taskEvent("auth-1", "in_progress", "2026-04-28T01:00:05Z", {
        title: "Refactor auth middleware",
        step: false,
      }),
      taskEvent("step-1", "open", "2026-04-28T01:00:10Z", {
        title: "Read the middleware",
        parent_id: "auth-1",
      }),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    expect(steps.map((s) => s.ticket_id)).toEqual(["step-1"]);
    expect(steps[0].is_step).toBe(true);
  });
});

describe("is_settled", () => {
  it("is false for active steps in the tail partition when agent is not idle", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    expect(steps[0].is_settled).toBe(false);
  });

  it("is true for active steps in the tail partition when agent is idle", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", true);
    expect(steps[0].is_settled).toBe(true);
  });

  it("is true for active steps in a past partition (has a successor)", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z", true);
    expect(steps[0].is_settled).toBe(true);
  });

  it("is false for done steps (is_settled only applies to active steps)", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:00:50Z", { summary: "Done." }),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z", true);
    expect(steps[0].status).toBe("done");
    expect(steps[0].is_settled).toBe(false);
  });
});

describe("picked-up-ticket attribution", () => {
  it("attributes a ticket to the window containing its earliest observed event", () => {
    const events: TranscriptEvent[] = [
      taskEvent("auth-1", "in_progress", "2026-04-28T03:00:30Z", {
        title: "Auth refactor",
        assignee: "agent-B",
        created_at: "2026-04-27T10:00:00Z",
      }),
    ];
    // Window 1: 02:00 -> 03:00 -- the ticket's created_at is way before
    // both windows, but first_observed_at is in window 2.
    const steps1 = stepsForWindow(events, "2026-04-28T02:00:00Z", "2026-04-28T03:00:00Z", true);
    expect(steps1).toEqual([]);
    // Window 2: 03:00 -> tail
    const steps2 = stepsForWindow(events, "2026-04-28T03:00:00Z", "", false);
    expect(steps2.map((s) => s.ticket_id)).toEqual(["auth-1"]);
  });
});

describe("eventsInTaskWindow", () => {
  it("returns only events between a step's started_at and closed_at", () => {
    const step: StepView = {
      ticket_id: "t1",
      title: "Step 1",
      status: "done",
      summary: "Did it",
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:20Z",
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: "2026-04-28T01:00:50Z",
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const body = [
      assistantMsg("2026-04-28T01:00:15Z", "before start"),
      toolUse("2026-04-28T01:00:25Z", "Read", "tc1"),
      toolUse("2026-04-28T01:00:45Z", "Edit", "tc2"),
      assistantMsg("2026-04-28T01:00:55Z", "after end"),
    ];
    const result = eventsInTaskWindow(step, body);
    expect(result.map((e) => e.event_id)).toEqual(["a-tc1", "a-tc2"]);
  });

  it("returns events through end when active_window_end is null", () => {
    const step: StepView = {
      ticket_id: "t1",
      title: "Active",
      status: "active",
      summary: null,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:20Z",
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: null,
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const body = [toolUse("2026-04-28T01:00:25Z", "Read", "tc1"), toolUse("2026-04-28T01:00:45Z", "Edit", "tc2")];
    expect(eventsInTaskWindow(step, body)).toHaveLength(2);
  });

  it("pulls in a trailing tool_result whose tool_use was in window", () => {
    const step: StepView = {
      ticket_id: "t1",
      title: "Step 1",
      status: "done",
      summary: "Did it",
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:20Z",
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: "2026-04-28T01:00:50Z",
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
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
    const body = [toolUse("2026-04-28T01:00:45Z", "Read", "tc-late"), toolResult("2026-04-28T01:00:51Z", "tc-late")];
    const result = eventsInTaskWindow(step, body);
    expect(result.map((e) => e.event_id)).toEqual(["a-tc-late", "r-tc-late"]);
  });

  it("caps an abandoned step's window at the next step's start when steps are provided", () => {
    const abandoned: StepView = {
      ticket_id: "step-a",
      title: "First, never closed",
      status: "active",
      summary: null,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const properlyClosed: StepView = {
      ticket_id: "step-b",
      title: "Second, closed cleanly",
      status: "done",
      summary: "Did the second step.",
      created_at: "2026-04-28T01:00:10Z",
      started_at: "2026-04-28T01:00:10Z",
      active_window_start: "2026-04-28T01:00:10Z",
      active_window_end: "2026-04-28T01:00:30Z",
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const steps = [abandoned, properlyClosed];
    const body = [
      toolUse("2026-04-28T01:00:05Z", "Read", "tc-a"),
      toolUse("2026-04-28T01:00:15Z", "Edit", "tc-b"),
      toolUse("2026-04-28T01:00:25Z", "Bash", "tc-c"),
    ];
    const withSteps = eventsInTaskWindow(abandoned, body, steps);
    expect(withSteps.map((e) => e.event_id)).toEqual(["a-tc-a"]);
    const withoutSteps = eventsInTaskWindow(abandoned, body);
    expect(withoutSteps).toHaveLength(3);
  });
});

describe("selectFinalMessages", () => {
  it("returns every text-only assistant_message outside step windows", () => {
    const a1 = assistantMsg("2026-04-28T01:00:10Z", "Here is the summary table...", "msg-summary");
    const a2 = assistantMsg("2026-04-28T01:00:20Z", "Waiting on your input.", "msg-waiting");
    const events = [a1, toolUse("2026-04-28T01:00:15Z", "Bash", "tc-1"), a2];
    expect(selectFinalMessages(events, []).map((e) => e.event_id)).toEqual(["msg-summary", "msg-waiting"]);
  });

  it("excludes tool-bearing assistant_messages even when they have text", () => {
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
    const empty = assistantMsg("2026-04-28T01:00:00Z", "", "a-empty");
    expect(selectFinalMessages([empty], [])).toEqual([]);
  });

  it("promotes the last in-window message when its step is done", () => {
    const doneStep: StepView = {
      ticket_id: "t1",
      title: "Investigate",
      status: "done",
      summary: "Found root cause, fix is X.",
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:01:00Z",
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const wrapup = assistantMsg("2026-04-28T01:00:50Z", "Found it -- the OAuth regex was too tight.", "msg-wrapup");
    expect(selectFinalMessages([wrapup], [doneStep]).map((e) => e.event_id)).toEqual(["msg-wrapup"]);
  });

  it("promotes the last in-window message when its step is settled", () => {
    const settledStep: StepView = {
      ticket_id: "t1",
      title: "Investigate",
      status: "active",
      summary: null,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: true,
    };
    const trailing = assistantMsg("2026-04-28T01:00:50Z", "Stuck -- need your call.", "msg-trailing");
    expect(selectFinalMessages([trailing], [settledStep]).map((e) => e.event_id)).toEqual(["msg-trailing"]);
  });

  it("does NOT surface a trailing message of an unsettled active step", () => {
    const openStep: StepView = {
      ticket_id: "t1",
      title: "Investigate",
      status: "active",
      summary: null,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const trailing = assistantMsg("2026-04-28T01:00:50Z", "Stuck -- need your call.", "msg-trailing");
    expect(selectFinalMessages([trailing], [openStep])).toEqual([]);
  });

  it("does NOT swallow a later message into an abandoned still-open step", () => {
    const abandoned: StepView = {
      ticket_id: "step-a",
      title: "First, never closed",
      status: "active",
      summary: null,
      created_at: "2026-04-28T01:00:00Z",
      started_at: "2026-04-28T01:00:00Z",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const properlyClosed: StepView = {
      ticket_id: "step-b",
      title: "Second, closed cleanly",
      status: "done",
      summary: "Did the second step.",
      created_at: "2026-04-28T01:00:10Z",
      started_at: "2026-04-28T01:00:10Z",
      active_window_start: "2026-04-28T01:00:10Z",
      active_window_end: "2026-04-28T01:00:30Z",
      is_step: true,
      parent_id: "",
      children: [],
      narration: null,
      is_settled: false,
    };
    const wrapup = assistantMsg("2026-04-28T01:00:40Z", "Both done -- here is the wrap-up.", "msg-wrapup");
    expect(selectFinalMessages([wrapup], [abandoned, properlyClosed]).map((e) => e.event_id)).toEqual(["msg-wrapup"]);
  });
});

describe("narration attribution", () => {
  it("populates narration with the latest text-only message inside an active step's window", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Do the thing", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
    ];
    const body = [
      assistantMsg("2026-04-28T01:00:10Z", "Trying approach A.", "a1"),
      assistantMsg("2026-04-28T01:00:20Z", "Approach A failed, trying B.", "a2"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    attributeNarration(steps, body);
    expect(steps).toHaveLength(1);
    expect(steps[0].narration).toBe("Approach A failed, trying B.");
  });

  it("leaves narration null on a closed step -- the summary owns the slot", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Do the thing", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
      taskEvent("t1", "closed", "2026-04-28T01:00:30Z", { summary: "Did the thing.", step: true }),
    ];
    const body = [assistantMsg("2026-04-28T01:00:10Z", "Mid-task narration.", "a1")];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    attributeNarration(steps, body);
    expect(steps[0].status).toBe("done");
    expect(steps[0].summary).toBe("Did the thing.");
    expect(steps[0].narration).toBeNull();
  });

  it("leaves narration null on a settled step -- the message is promoted to final instead", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Investigate", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
    ];
    const body = [
      assistantMsg("2026-04-28T01:00:10Z", "Looking into it.", "a1"),
      assistantMsg("2026-04-28T01:00:20Z", "Stuck -- need your input.", "a2"),
    ];
    // Settled = past partition or agent idle
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T02:00:00Z", true);
    attributeNarration(steps, body);
    expect(steps[0].is_settled).toBe(true);
    expect(steps[0].narration).toBeNull();
  });
});
