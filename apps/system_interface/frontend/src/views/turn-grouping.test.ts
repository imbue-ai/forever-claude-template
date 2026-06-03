import { describe, expect, it } from "vitest";
import type {
  TranscriptEvent,
  TaskEvent,
  ToolResultEvent,
  AssistantMessageEvent,
  UserMessageEvent,
} from "../models/Response";
import type { StepEnrichment, StepNode, TimelineItem } from "./turn-grouping";
import { buildEnrichment, buildSections } from "./turn-grouping";

// --- Event builders ---

function userMsg(ts: string, content: string, id = `u-${ts}`): UserMessageEvent {
  return { timestamp: ts, type: "user_message", event_id: id, source: "test", role: "user", content };
}

function assistantText(ts: string, text: string, id = `a-${ts}`): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: id,
    source: "test",
    model: "m",
    text,
    tool_calls: [],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

/** A non-tk work message: one tool call, no prose. */
function workMsg(ts: string, toolName: string, callId: string, id = `a-${callId}`): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: id,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_preview: `{"path":"x"}` }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

/** A tk lifecycle command as it appears in the transcript: a Bash tool call
 *  whose input_preview is the JSON-encoded command. */
function tkMsg(ts: string, command: string, callId: string, id = `a-${callId}`): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: id,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: "Bash", input_preview: `{"command":"${command}"}` }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

function result(ts: string, callId: string, output: string): ToolResultEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "test",
    output,
    is_error: false,
  };
}

function taskEvent(
  ticketId: string,
  status: "open" | "in_progress" | "closed",
  ts: string,
  extras: Partial<TaskEvent> = {},
): TranscriptEvent {
  return {
    timestamp: ts,
    type: "task_event",
    event_id: `${ticketId}-${status}`,
    source: "tk",
    ticket_id: ticketId,
    title: extras.title ?? "Some step",
    status,
    created_at: extras.created_at ?? ts,
    summary: extras.summary ?? null,
    summary_at: extras.summary_at ?? null,
    step: extras.step ?? true,
    parent_id: extras.parent_id ?? "",
    assignee: extras.assignee ?? "",
  };
}

function enrich(entries: Record<string, Partial<StepEnrichment>>): Map<string, StepEnrichment> {
  const m = new Map<string, StepEnrichment>();
  for (const [id, e] of Object.entries(entries)) {
    m.set(id, {
      title: e.title ?? id,
      summary: e.summary ?? null,
      status: e.status ?? "in_progress",
      created_at: e.created_at ?? "2026-04-28T01:00:00.000000Z",
    });
  }
  return m;
}

/** Build the toolResults map from the event list (as ChatPanel does) and run. */
function run(events: TranscriptEvent[], enrichment: Map<string, StepEnrichment> = new Map(), agentIsIdle = true) {
  const toolResults = new Map<string, ToolResultEvent>();
  for (const e of events) {
    if (e.type === "tool_result") toolResults.set(e.tool_call_id, e);
  }
  return buildSections(events, toolResults, enrichment, agentIsIdle);
}

function stepItems(items: TimelineItem[]): StepNode[] {
  return items.filter((i): i is { kind: "step"; step: StepNode } => i.kind === "step").map((i) => i.step);
}

describe("buildEnrichment", () => {
  it("folds task_events by id, keeps only steps, latest status wins, summary on close", () => {
    const events = [
      taskEvent("s1", "open", "2026-04-28T01:00:00Z", { title: "First", created_at: "2026-04-28T01:00:00Z" }),
      taskEvent("s1", "in_progress", "2026-04-28T01:00:05Z", { title: "First", created_at: "2026-04-28T01:00:00Z" }),
      taskEvent("s1", "closed", "2026-04-28T01:00:10Z", {
        title: "First",
        created_at: "2026-04-28T01:00:00Z",
        summary: "Did the thing",
      }),
      taskEvent("reg", "open", "2026-04-28T01:00:00Z", { step: false }),
    ];
    const table = buildEnrichment(events);
    expect(table.has("reg")).toBe(false);
    const s1 = table.get("s1")!;
    expect(s1.title).toBe("First");
    expect(s1.status).toBe("closed");
    expect(s1.summary).toBe("Did the thing");
    expect(s1.created_at).toBe("2026-04-28T01:00:00Z");
  });
});

describe("bug fixes", () => {
  // BUG 1: work done before the first step must not vanish.
  it("renders tool calls done before the first step in an ungrouped item", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      workMsg("2026-04-28T01:00:05Z", "Read", "tc-read"),
      result("2026-04-28T01:00:06Z", "tc-read", "file contents"),
      tkMsg("2026-04-28T01:00:10Z", "tk start s1", "tc-start"),
      result("2026-04-28T01:00:11Z", "tc-start", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:15Z", "Edit", "tc-edit"),
      result("2026-04-28T01:00:16Z", "tc-edit", "ok"),
    ];
    const sections = run(events, enrich({ s1: { title: "Do it" } }));
    expect(sections).toHaveLength(1);
    const items = sections[0].items;
    // The pre-step Read is an ungrouped item that comes BEFORE the step node.
    expect(items[0].kind).toBe("ungrouped");
    const ung = items[0] as { kind: "ungrouped"; events: AssistantMessageEvent[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["a-tc-read"]);
    expect(items[1].kind).toBe("step");
    const step = (items[1] as { kind: "step"; step: StepNode }).step;
    expect(step.ticket_id).toBe("s1");
    expect(step.events.map((e) => e.event_id)).toEqual(["a-tc-edit"]);
  });

  // BUG 2: a started step renders in its transcript position, never hoisted up.
  it("positions an in-progress step after earlier closed steps, not at the top", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start a", "t-a-start"),
      result("2026-04-28T01:00:01Z", "t-a-start", "Updated a -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w-a"),
      result("2026-04-28T01:00:02Z", "w-a", "ok"),
      tkMsg("2026-04-28T01:00:03Z", "tk close a", "t-a-close"),
      result("2026-04-28T01:00:03Z", "t-a-close", "Updated a -> closed"),
      tkMsg("2026-04-28T01:00:04Z", "tk start b", "t-b-start"),
      result("2026-04-28T01:00:04Z", "t-b-start", "Updated b -> in_progress"),
      workMsg("2026-04-28T01:00:05Z", "Edit", "w-b"),
      result("2026-04-28T01:00:05Z", "w-b", "ok"),
    ];
    const sections = run(events, enrich({ a: {}, b: {} }), /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["a", "b"]);
    expect(steps[0].status).toBe("done");
    expect(steps[1].status).toBe("active");
    expect(steps[1].is_frontier).toBe(true);
  });
});

describe("grouping and status", () => {
  it("groups a step's work and shows its close summary when done", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:01Z", "t1", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      tkMsg("2026-04-28T01:00:03Z", "tk close s1", "t2"),
      result("2026-04-28T01:00:03Z", "t2", "Updated s1 -> closed"),
    ];
    const sections = run(events, enrich({ s1: { title: "Fix it", summary: "Fixed the bug", status: "closed" } }));
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].title).toBe("Fix it");
    expect(steps[0].status).toBe("done");
    expect(steps[0].summary).toBe("Fixed the bug");
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1"]);
  });

  it("does not render tk lifecycle commands as work", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:01Z", "t1", "Updated s1 -> in_progress"),
    ];
    const sections = run(events, enrich({ s1: {} }));
    const steps = stepItems(sections[0].items);
    expect(steps[0].events).toHaveLength(0);
    // No ungrouped items: the tk call was consumed, not rendered.
    expect(sections[0].items.filter((i) => i.kind === "ungrouped")).toHaveLength(0);
  });
});

describe("reply promotion", () => {
  it("promotes a wrap-up reply written before the closing tk close (rule 7a)", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:01Z", "t1", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      assistantText("2026-04-28T01:00:03Z", "All done -- want a test?", "reply"),
      tkMsg("2026-04-28T01:00:04Z", "tk close s1", "t2"),
      result("2026-04-28T01:00:04Z", "t2", "Updated s1 -> closed"),
    ];
    const sections = run(events, enrich({ s1: { status: "closed" } }));
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["reply"]);
    // The reply is not buried in the step.
    const steps = stepItems(sections[0].items);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1"]);
  });

  it("keeps mid-work narration in the step, not as the reply", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:01Z", "t1", "Updated s1 -> in_progress"),
      assistantText("2026-04-28T01:00:02Z", "Found it, patching now.", "narr"),
      workMsg("2026-04-28T01:00:03Z", "Edit", "w1"),
      result("2026-04-28T01:00:03Z", "w1", "ok"),
      assistantText("2026-04-28T01:00:04Z", "Done.", "reply"),
    ];
    const sections = run(events, enrich({ s1: {} }));
    const steps = stepItems(sections[0].items);
    expect(steps[0].narration).toBe("Found it, patching now.");
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["reply"]);
  });

  it("treats prose before the first step as an ungrouped (leading) item", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      assistantText("2026-04-28T01:00:01Z", "Sure, tracing the auth path.", "lead"),
      tkMsg("2026-04-28T01:00:02Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:02Z", "t1", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:03Z", "Edit", "w1"),
      result("2026-04-28T01:00:03Z", "w1", "ok"),
    ];
    const sections = run(events, enrich({ s1: {} }));
    const items = sections[0].items;
    expect(items[0].kind).toBe("ungrouped");
    const ung = items[0] as { kind: "ungrouped"; events: AssistantMessageEvent[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["lead"]);
    expect(sections[0].trailing_reply).toHaveLength(0);
  });
});

describe("carryover", () => {
  it("re-renders a still-open step at the top of the next turn with frozen prior state", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "first", "u1"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:01Z", "t1", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      userMsg("2026-04-28T01:00:10Z", "second", "u2"),
      workMsg("2026-04-28T01:00:11Z", "Edit", "w2"),
      result("2026-04-28T01:00:11Z", "w2", "ok"),
      tkMsg("2026-04-28T01:00:12Z", "tk close s1", "t2"),
      result("2026-04-28T01:00:12Z", "t2", "Updated s1 -> closed"),
    ];
    const sections = run(events, enrich({ s1: { status: "closed" } }));
    expect(sections).toHaveLength(2);

    const s1FirstTurn = stepItems(sections[0].items)[0];
    expect(s1FirstTurn.is_carryover).toBe(false);
    expect(s1FirstTurn.status).toBe("active"); // frozen: never flips to done here
    expect(s1FirstTurn.events.map((e) => e.event_id)).toEqual(["a-w1"]);

    const s1SecondTurn = stepItems(sections[1].items)[0];
    expect(sections[1].items[0].kind).toBe("step"); // at the top
    expect(s1SecondTurn.is_carryover).toBe(true);
    expect(s1SecondTurn.status).toBe("done");
    expect(s1SecondTurn.events.map((e) => e.event_id)).toEqual(["a-w2"]);
  });
});

describe("pending roster", () => {
  it("appends never-started steps as pending placeholders at the tail, in created order", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:01Z", "t1", "Updated s1 -> in_progress"),
    ];
    const enrichment = enrich({
      s1: { status: "in_progress" },
      s2: { title: "Second", status: "open", created_at: "2026-04-28T01:00:00.000002Z" },
      s3: { title: "Third", status: "open", created_at: "2026-04-28T01:00:00.000001Z" },
    });
    const sections = run(events, enrichment, /* idle */ false);
    const steps = stepItems(sections[0].items);
    // s1 active first; pending s3 then s2 (by created), placeholders at the tail.
    expect(steps.map((s) => s.ticket_id)).toEqual(["s1", "s3", "s2"]);
    expect(steps[1].status).toBe("pending");
    expect(steps[2].status).toBe("pending");
    expect(steps[0].is_frontier).toBe(true);
  });

  it("does not show pending placeholders in a non-tail section", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "first", "u1"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "t1"),
      result("2026-04-28T01:00:01Z", "t1", "Updated s1 -> in_progress"),
      tkMsg("2026-04-28T01:00:02Z", "tk close s1", "t1c"),
      result("2026-04-28T01:00:02Z", "t1c", "Updated s1 -> closed"),
      userMsg("2026-04-28T01:00:10Z", "second", "u2"),
    ];
    const enrichment = enrich({ s1: { status: "closed" }, sp: { status: "open" } });
    const sections = run(events, enrichment);
    // The pending sp shows only in the tail (second) section.
    expect(stepItems(sections[0].items).map((s) => s.ticket_id)).toEqual(["s1"]);
    expect(stepItems(sections[1].items).map((s) => s.ticket_id)).toEqual(["sp"]);
  });
});
