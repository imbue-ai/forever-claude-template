import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import type { StepView } from "./turn-grouping";
import {
  buildTaskRecords,
  buildSectionSteps,
  stepActiveInWindow,
  attributeNarration,
  eventsInTaskWindow,
  classifyTopLevelMessages,
  placeStopHookChips,
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

function toolResultEvent(ts: string, callId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    output: "ok",
  };
}

/** Build a StepView for placement/narration tests. */
function stepView(overrides: Partial<StepView> & Pick<StepView, "ticket_id" | "status">): StepView {
  return {
    title: overrides.title ?? "Step",
    summary: overrides.summary ?? null,
    created_at: overrides.created_at ?? overrides.active_window_start ?? "2026-04-28T01:00:00Z",
    started_at: overrides.started_at ?? overrides.active_window_start ?? null,
    active_window_start: overrides.active_window_start ?? null,
    active_window_end: overrides.active_window_end ?? null,
    is_step: overrides.is_step ?? true,
    parent_id: overrides.parent_id ?? "",
    children: overrides.children ?? [],
    narration: overrides.narration ?? null,
    is_settled: overrides.is_settled ?? false,
    ...overrides,
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
  return buildSectionSteps(buildTaskRecords(events), start_ts, end_ts, is_settled);
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

  it("clamps a step's status to each partition: still active in the earlier block, done in the later one", () => {
    // The step is in_progress at the 01:01:00 boundary and only closes at
    // 01:01:30, in the second partition. The earlier block must render it as
    // still-active (not retroactively "done"), and its summary/closed_at must
    // not leak backwards; the later block renders the close.
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Step 1" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:01:30Z", {
        summary: "Wrapped up step 1",
        summary_at: "2026-04-28T01:01:25Z",
      }),
    ];
    // First partition: 01:00:00 -> 01:01:00 (closes later, so still active here)
    const steps1 = stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z", true);
    expect(steps1).toHaveLength(1);
    expect(steps1[0]).toMatchObject({ ticket_id: "t1", status: "active", summary: null, active_window_end: null });

    // Second partition: 01:01:00 -> "" (tail) -- the close lands here
    const steps2 = stepsForWindow(events, "2026-04-28T01:01:00Z", "", false);
    expect(steps2).toHaveLength(1);
    expect(steps2[0]).toMatchObject({ ticket_id: "t1", status: "done", summary: "Wrapped up step 1" });
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

  it("spins only the frontier step in the running tail; an earlier still-open step settles", () => {
    // The agent is on the most-recently-started step. An earlier step left
    // open is not the frontier, so it must not show a live spinner above it.
    const events: TranscriptEvent[] = [
      taskEvent("a", "open", "2026-04-28T01:00:05Z", { created_at: "2026-04-28T01:00:05Z", title: "First" }),
      taskEvent("a", "in_progress", "2026-04-28T01:00:10Z"),
      taskEvent("b", "open", "2026-04-28T01:00:15Z", { created_at: "2026-04-28T01:00:15Z", title: "Second" }),
      taskEvent("b", "in_progress", "2026-04-28T01:00:20Z"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    const a = steps.find((s) => s.ticket_id === "a")!;
    const b = steps.find((s) => s.ticket_id === "b")!;
    expect(a.status).toBe("active");
    expect(a.is_settled).toBe(true); // superseded -> static ring, no spinner
    expect(b.status).toBe("active");
    expect(b.is_settled).toBe(false); // frontier -> live spinner
  });

  it("settles an earlier open step even when the later step that superseded it is already done", () => {
    // The screenshot bug: step 'a' spins while a *later* step 'b' is already
    // done. Once a later step started -- even one that has since closed -- the
    // earlier open step was left behind and must render static, not spinning.
    const events: TranscriptEvent[] = [
      taskEvent("a", "open", "2026-04-28T01:00:05Z", { created_at: "2026-04-28T01:00:05Z", title: "Start service" }),
      taskEvent("a", "in_progress", "2026-04-28T01:00:10Z"),
      taskEvent("b", "open", "2026-04-28T01:00:15Z", { created_at: "2026-04-28T01:00:15Z", title: "Build UI" }),
      taskEvent("b", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("b", "closed", "2026-04-28T01:00:40Z", { summary: "Built the UI." }),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    const a = steps.find((s) => s.ticket_id === "a")!;
    const b = steps.find((s) => s.ticket_id === "b")!;
    expect(a.status).toBe("active");
    expect(a.is_settled).toBe(true); // not the frontier (b started later) -> static
    expect(b.status).toBe("done");
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

describe("classifyTopLevelMessages", () => {
  it("excludes tool-bearing and empty-text assistant_messages", () => {
    const withTextAndTools: TranscriptEvent = {
      timestamp: "2026-04-28T01:00:00Z",
      type: "assistant_message",
      event_id: "a-mixed",
      source: "test",
      text: "Calling out to a tool.",
      tool_calls: [{ tool_call_id: "tc-x", tool_name: "Bash", input_preview: "{}" }],
    };
    const empty = assistantMsg("2026-04-28T01:00:05Z", "", "a-empty");
    const placed = classifyTopLevelMessages([withTextAndTools, empty], []);
    expect(placed).toEqual({ leading: [], inter_step: [], trailing: [] });
  });

  // A2 (close -> speak, the ideal): the post-close reply is trailing.
  it("promotes a post-close message to the trailing reply (A2)", () => {
    const doneStep = stepView({
      ticket_id: "t1",
      status: "done",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:50Z",
      summary: "Found the null-check bug and patched it.",
    });
    const reply = assistantMsg("2026-04-28T01:00:55Z", "Fixed it -- want a regression test?", "msg-reply");
    const placed = classifyTopLevelMessages([reply], [doneStep]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-reply"]);
    expect(placed.leading).toEqual([]);
    expect(placed.inter_step).toEqual([]);
  });

  // A3 (speak -> close): the pre-close message stays in-step, not promoted.
  it("keeps a pre-close message in-step, not promoted (A3)", () => {
    const doneStep = stepView({
      ticket_id: "t1",
      status: "done",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:50Z",
      summary: "Found the null-check bug and patched it.",
    });
    const preClose = assistantMsg("2026-04-28T01:00:45Z", "Fixed it -- want a regression test?", "msg-pre");
    const placed = classifyTopLevelMessages([preClose], [doneStep]);
    expect(placed).toEqual({ leading: [], inter_step: [], trailing: [] });
  });

  // A4 (speak -> close -> speak): only the post-close message is promoted.
  it("promotes only the post-close message when text brackets the close (A4)", () => {
    const doneStep = stepView({
      ticket_id: "t1",
      status: "done",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:50Z",
    });
    const pre = assistantMsg("2026-04-28T01:00:45Z", "Patched it.", "msg-pre");
    const post = assistantMsg("2026-04-28T01:00:55Z", "Want a regression test?", "msg-post");
    const placed = classifyTopLevelMessages([pre, post], [doneStep]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-post"]);
    expect(placed.inter_step).toEqual([]);
    expect(placed.leading).toEqual([]);
  });

  // B2 (open step, speak after the last tool): reply promotes below.
  it("promotes a message after the last tool when the step never closed (B2)", () => {
    const openStep = stepView({
      ticket_id: "t1",
      status: "active",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_settled: true,
    });
    const body = [
      toolUse("2026-04-28T01:00:10Z", "Edit", "tc-1"),
      toolResultEvent("2026-04-28T01:00:11Z", "tc-1"),
      assistantMsg("2026-04-28T01:00:20Z", "Fixed the null-check. Want a test?", "msg-reply"),
    ];
    const placed = classifyTopLevelMessages(body, [openStep]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-reply"]);
    expect(placed.inter_step).toEqual([]);
    expect(placed.leading).toEqual([]);
  });

  // B3 (open, speak, tools, speak): mid-work narration stays in-step; only
  // the trailing message (after the last tool) is promoted.
  it("promotes only the trailing message; mid-work narration stays in-step (B3)", () => {
    const openStep = stepView({
      ticket_id: "t1",
      status: "active",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_settled: true,
    });
    const narration = assistantMsg("2026-04-28T01:00:05Z", "Found the bug -- patching now.", "msg-narr");
    const reply = assistantMsg("2026-04-28T01:00:30Z", "Done. Want a test?", "msg-reply");
    const body = [narration, toolUse("2026-04-28T01:00:15Z", "Edit", "tc-1"), reply];
    const placed = classifyTopLevelMessages(body, [openStep]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-reply"]);
    expect(placed.leading).toEqual([]);
    expect(placed.inter_step).toEqual([]);
  });

  // B4 (first step closed, second open, trailing reply): scan stops at the
  // second step's tool activity, never reaching the first close.
  it("promotes the trailing reply across a closed-then-open step pair (B4)", () => {
    const closed = stepView({
      ticket_id: "step-a",
      status: "done",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:30Z",
    });
    const open = stepView({
      ticket_id: "step-b",
      status: "active",
      active_window_start: "2026-04-28T01:00:40Z",
      active_window_end: null,
      is_settled: true,
    });
    const body = [
      toolUse("2026-04-28T01:00:50Z", "Bash", "tc-1"),
      assistantMsg("2026-04-28T01:01:00Z", "Started the tests -- pytest or unittest?", "msg-reply"),
    ];
    const placed = classifyTopLevelMessages(body, [closed, open]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-reply"]);
    expect(placed.inter_step).toEqual([]);
    expect(placed.leading).toEqual([]);
  });

  // C1 (leading): prose before the first step renders above the timeline.
  it("classifies prose before the first step as leading", () => {
    const step = stepView({
      ticket_id: "t1",
      status: "done",
      active_window_start: "2026-04-28T01:00:10Z",
      active_window_end: "2026-04-28T01:00:50Z",
    });
    const lead = assistantMsg("2026-04-28T01:00:05Z", "Sure -- tracing the auth path first.", "msg-lead");
    const placed = classifyTopLevelMessages([lead], [step]);
    expect(placed.leading.map((e) => e.event_id)).toEqual(["msg-lead"]);
    expect(placed.trailing).toEqual([]);
    expect(placed.inter_step).toEqual([]);
  });

  // C2 (full composite): leading + inter-step + trailing in one section.
  it("classifies leading, inter-step, and trailing prose together (C2)", () => {
    const step1 = stepView({
      ticket_id: "step-1",
      status: "done",
      active_window_start: "2026-04-28T01:00:10Z",
      active_window_end: "2026-04-28T01:00:30Z",
    });
    const step2 = stepView({
      ticket_id: "step-2",
      status: "done",
      active_window_start: "2026-04-28T01:00:50Z",
      active_window_end: "2026-04-28T01:01:10Z",
    });
    const lead = assistantMsg("2026-04-28T01:00:05Z", "On it -- tracing the auth path.", "msg-lead");
    const inter = assistantMsg("2026-04-28T01:00:40Z", "Refresh path has the same flaw -- next.", "msg-inter");
    const trail = assistantMsg("2026-04-28T01:01:20Z", "Both paths fixed. Want tests?", "msg-trail");
    const placed = classifyTopLevelMessages([lead, inter, trail], [step1, step2]);
    expect(placed.leading.map((e) => e.event_id)).toEqual(["msg-lead"]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-trail"]);
    expect(placed.inter_step).toHaveLength(1);
    expect(placed.inter_step[0].event.event_id).toBe("msg-inter");
    expect(placed.inter_step[0].before_step_id).toBe("step-2");
  });

  // A1 (close, silent): nothing trailing.
  it("returns nothing when the agent closes and stays silent (A1)", () => {
    const doneStep = stepView({
      ticket_id: "t1",
      status: "done",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:50Z",
      summary: "Found the null-check bug and patched it.",
    });
    expect(classifyTopLevelMessages([], [doneStep])).toEqual({ leading: [], inter_step: [], trailing: [] });
  });

  // Issue 4: a wrap-up reply emitted at end of turn while a step is still
  // open must stay promoted after the next user message arrives and the step
  // later closes. With the per-partition clamp, the step renders active (no
  // future closed_at) in the earlier block, so the reply boundary stays at
  // the last tool call and the wrap-up remains the trailing reply.
  it("keeps the end-of-turn reply trailing when its step only closes in a later partition", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Work" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      toolUse("2026-04-28T01:00:30Z", "Edit", "tc-1"),
      assistantMsg("2026-04-28T01:00:50Z", "Done -- want me to also add a test?", "msg-reply"),
      // The step is only closed in the next partition (after a later user msg).
      taskEvent("t1", "closed", "2026-04-28T01:02:00Z", { summary: "Did the work." }),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z", true);
    expect(steps[0]).toMatchObject({ status: "active", active_window_end: null });
    const body = bodyEventsInWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T01:01:00Z");
    const placed = classifyTopLevelMessages(body, steps);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-reply"]);
  });

  it("promotes a multi-message trailing reply run as a single trailing block", () => {
    const doneStep = stepView({
      ticket_id: "t1",
      status: "done",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:30Z",
    });
    const r1 = assistantMsg("2026-04-28T01:00:40Z", "Here's the summary.", "msg-1");
    const r2 = assistantMsg("2026-04-28T01:00:45Z", "And one caveat.", "msg-2");
    const placed = classifyTopLevelMessages([r1, r2], [doneStep]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-1", "msg-2"]);
  });
});

describe("stop-hook reply segments (issue 3)", () => {
  const doneStep = (): StepView =>
    stepView({
      ticket_id: "t1",
      status: "done",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: "2026-04-28T01:00:20Z",
      summary: "Did the work.",
    });

  it("surfaces BOTH the pre-hook wrap-up and the post-hook reply when post-hook tool work follows", () => {
    // Without segmentation, the post-hook tool call pushes the reply boundary
    // past the pre-hook reply, collapsing it and promoting the post-hook prose
    // as the sole headline. With per-segment scanning both surface.
    const body = [
      toolUse("2026-04-28T01:00:10Z", "Edit", "tc-pre"),
      assistantMsg("2026-04-28T01:00:30Z", "Done -- here is the summary.", "msg-pre"),
      userMsg("2026-04-28T01:00:40Z", "Stop hook feedback:\nRun /autofix.", "u-hook"),
      toolUse("2026-04-28T01:00:50Z", "Bash", "tc-post"),
      assistantMsg("2026-04-28T01:01:00Z", "Autofix found nothing; working tree clean.", "msg-post"),
    ];
    const placed = classifyTopLevelMessages(body, [doneStep()]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-pre", "msg-post"]);
    expect(placed.leading).toEqual([]);
    expect(placed.inter_step).toEqual([]);
  });

  it("surfaces a post-hook reply that has no further tool work, without collapsing the pre-hook reply", () => {
    const body = [
      toolUse("2026-04-28T01:00:10Z", "Edit", "tc-pre"),
      assistantMsg("2026-04-28T01:00:30Z", "Done -- want a test?", "msg-pre"),
      userMsg("2026-04-28T01:00:40Z", "Stop hook feedback:\nReview the conversation.", "u-hook"),
      assistantMsg("2026-04-28T01:00:50Z", "Nothing to change; ready for your go-ahead.", "msg-post"),
    ];
    const placed = classifyTopLevelMessages(body, [doneStep()]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-pre", "msg-post"]);
  });

  it("keeps post-hook mid-work narration in-step; only the post-hook trailing reply is promoted", () => {
    // After the hook the agent narrates, does more tool work, then replies.
    // The narration (followed by a post-hook tool) is not the segment's reply.
    const openStep = stepView({
      ticket_id: "t1",
      status: "active",
      active_window_start: "2026-04-28T01:00:00Z",
      active_window_end: null,
      is_settled: true,
    });
    const body = [
      toolUse("2026-04-28T01:00:10Z", "Edit", "tc-pre"),
      assistantMsg("2026-04-28T01:00:30Z", "Finished the change.", "msg-pre"),
      userMsg("2026-04-28T01:00:40Z", "Stop hook feedback:\nRun /autofix.", "u-hook"),
      assistantMsg("2026-04-28T01:00:45Z", "Re-running autofix now.", "msg-post-narr"),
      toolUse("2026-04-28T01:00:50Z", "Bash", "tc-post"),
      assistantMsg("2026-04-28T01:01:00Z", "All clean.", "msg-post-reply"),
    ];
    const placed = classifyTopLevelMessages(body, [openStep]);
    expect(placed.trailing.map((e) => e.event_id)).toEqual(["msg-pre", "msg-post-reply"]);
    // msg-post-narr is mid-work in the post-hook segment, not a reply.
    expect(placed.trailing.map((e) => e.event_id)).not.toContain("msg-post-narr");
  });
});

describe("placeStopHookChips", () => {
  const hookMsg = (ts: string, id: string): TranscriptEvent => userMsg(ts, "Stop hook feedback:\nRun /autofix.", id);

  it("places the chip before the first step that starts after the hook fired", () => {
    const pre = stepView({ ticket_id: "pre", status: "done", active_window_start: "2026-04-28T01:00:00Z" });
    const post = stepView({ ticket_id: "post", status: "active", active_window_start: "2026-04-28T01:00:40Z" });
    const body = [hookMsg("2026-04-28T01:00:30Z", "u-hook")];
    const placed = placeStopHookChips(body, [pre, post]);
    expect(placed).toHaveLength(1);
    expect(placed[0].event.event_id).toBe("u-hook");
    expect(placed[0].before_step_id).toBe("post");
  });

  it("places the chip after the last step when no step starts after the hook", () => {
    const a = stepView({ ticket_id: "a", status: "done", active_window_start: "2026-04-28T01:00:00Z" });
    const b = stepView({ ticket_id: "b", status: "done", active_window_start: "2026-04-28T01:00:10Z" });
    const body = [hookMsg("2026-04-28T01:00:50Z", "u-hook")];
    const placed = placeStopHookChips(body, [a, b]);
    expect(placed).toHaveLength(1);
    expect(placed[0].before_step_id).toBe(""); // render at the bottom of the timeline
  });

  it("ignores non-stop-hook user messages and assistant messages", () => {
    const step = stepView({ ticket_id: "t1", status: "done", active_window_start: "2026-04-28T01:00:00Z" });
    const body = [
      userMsg("2026-04-28T01:00:10Z", "Base directory for this skill: /x/skills/foo/", "u-skill"),
      assistantMsg("2026-04-28T01:00:20Z", "some prose", "a-1"),
      userMsg("2026-04-28T01:00:30Z", "a real user prompt", "u-real"),
    ];
    expect(placeStopHookChips(body, [step])).toEqual([]);
  });

  it("places multiple hooks each before their following step", () => {
    const s1 = stepView({ ticket_id: "s1", status: "done", active_window_start: "2026-04-28T01:00:00Z" });
    const s2 = stepView({ ticket_id: "s2", status: "done", active_window_start: "2026-04-28T01:00:40Z" });
    const s3 = stepView({ ticket_id: "s3", status: "active", active_window_start: "2026-04-28T01:01:20Z" });
    const body = [hookMsg("2026-04-28T01:00:30Z", "u-h1"), hookMsg("2026-04-28T01:01:10Z", "u-h2")];
    const placed = placeStopHookChips(body, [s1, s2, s3]);
    expect(placed.map((p) => [p.event.event_id, p.before_step_id])).toEqual([
      ["u-h1", "s2"],
      ["u-h2", "s3"],
    ]);
  });
});

describe("narration attribution", () => {
  it("uses the latest in-window text-only message that is FOLLOWED by tool activity", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Do the thing", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
    ];
    const body = [
      assistantMsg("2026-04-28T01:00:10Z", "Trying approach A.", "a1"),
      toolUse("2026-04-28T01:00:12Z", "Read", "tc-1"),
      assistantMsg("2026-04-28T01:00:20Z", "Approach A failed, trying B.", "a2"),
      toolUse("2026-04-28T01:00:22Z", "Edit", "tc-2"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    attributeNarration(steps, body);
    expect(steps).toHaveLength(1);
    expect(steps[0].narration).toBe("Approach A failed, trying B.");
  });

  it("does NOT use a trailing message that is not followed by tool activity (it is the reply)", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Do the thing", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
    ];
    const body = [
      assistantMsg("2026-04-28T01:00:10Z", "Mid-work narration.", "a1"),
      toolUse("2026-04-28T01:00:12Z", "Read", "tc-1"),
      assistantMsg("2026-04-28T01:00:30Z", "All done -- this is the reply.", "a2"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    attributeNarration(steps, body);
    // The narration is the FIRST message (followed by a tool), not the
    // trailing reply (which has no tool after it).
    expect(steps[0].narration).toBe("Mid-work narration.");
  });

  it("still surfaces narration on a settled (idle, unclosed) step -- decoupled from is_settled", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Investigate", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
    ];
    const body = [
      assistantMsg("2026-04-28T01:00:10Z", "Found the bug -- patching now.", "a1"),
      toolUse("2026-04-28T01:00:15Z", "Edit", "tc-1"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "2026-04-28T02:00:00Z", true);
    attributeNarration(steps, body);
    expect(steps[0].is_settled).toBe(true);
    expect(steps[0].narration).toBe("Found the bug -- patching now.");
  });

  it("leaves narration null on a closed step -- the summary owns the slot", () => {
    const events: TranscriptEvent[] = [
      taskEvent("t1", "open", "2026-04-28T01:00:05Z", { title: "Do the thing", step: true }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:06Z", { step: true }),
      taskEvent("t1", "closed", "2026-04-28T01:00:30Z", { summary: "Did the thing.", step: true }),
    ];
    const body = [
      assistantMsg("2026-04-28T01:00:10Z", "Mid-task narration.", "a1"),
      toolUse("2026-04-28T01:00:12Z", "Read", "tc-1"),
    ];
    const steps = stepsForWindow(events, "2026-04-28T01:00:00Z", "", false);
    attributeNarration(steps, body);
    expect(steps[0].status).toBe("done");
    expect(steps[0].summary).toBe("Did the thing.");
    expect(steps[0].narration).toBeNull();
  });
});
