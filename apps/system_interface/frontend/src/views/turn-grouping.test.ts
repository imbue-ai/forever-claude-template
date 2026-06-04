import { describe, expect, it } from "vitest";
import type {
  TranscriptEvent,
  ToolResultEvent,
  AssistantMessageEvent,
  UserMessageEvent,
  StepEnrichment,
} from "../models/Response";
import type { StepNode, TimelineItem } from "./turn-grouping";
import { buildSections } from "./turn-grouping";

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

describe("step ordering edge cases", () => {
  // BUG 3 (bababa repro): a step closed without ever being started -- and thus
  // with no work events -- must still render at its transcript position, not be
  // shoved below a later step that does have work. Here p5jc closes (no work)
  // before ts53 starts and does work; p5jc must sit between nzb4 and ts53.
  it("positions a no-work step at its transition spot, not below a later working step", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "set up my inbox view"),
      tkMsg("2026-04-28T01:00:01Z", "tk start nzb4", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated nzb4 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Bash", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      tkMsg("2026-04-28T01:00:03Z", "tk close nzb4", "k2"),
      result("2026-04-28T01:00:03Z", "k2", "Updated nzb4 -> closed"),
      // p5jc is closed directly, with no start and no work.
      tkMsg("2026-04-28T01:00:04Z", "tk close p5jc", "k3"),
      result("2026-04-28T01:00:04Z", "k3", "Updated p5jc -> closed"),
      tkMsg("2026-04-28T01:00:05Z", "tk start ts53", "k4"),
      result("2026-04-28T01:00:05Z", "k4", "Updated ts53 -> in_progress"),
      assistantText("2026-04-28T01:00:06Z", "Now let me fetch a sample.", "narr"),
      workMsg("2026-04-28T01:00:07Z", "Bash", "w2"),
      result("2026-04-28T01:00:07Z", "w2", "ok"),
    ];
    const sections = run(
      events,
      enrich({ nzb4: { status: "closed" }, p5jc: { status: "closed" }, ts53: { status: "in_progress" } }),
      /* idle */ false,
    );
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["nzb4", "p5jc", "ts53"]);
    expect(steps[1].status).toBe("done"); // p5jc: done, in the middle
    expect(steps[2].status).toBe("active"); // ts53: active, at the bottom
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

describe("audit regressions", () => {
  // FIX C: a Bash command that merely mentions a tk verb must render as work,
  // not be misclassified as a tk lifecycle command and silently dropped.
  it("renders a non-tk command that mentions a tk verb as work (grouped under the open step)", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated s1 -> in_progress"),
      // A real git command whose message mentions "tk close" -- not a tk command.
      tkMsg("2026-04-28T01:00:02Z", "git commit -m 'tk close the bug'", "gc"),
      result("2026-04-28T01:00:02Z", "gc", "[main abc] tk close the bug"),
    ];
    const sections = run(events, enrich({ s1: { status: "in_progress" } }), /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-gc"]);
  });

  it("keeps a tk-mentioning command visible (ungrouped) when no step is open", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "echo 'run tk start later'", "e1"),
      result("2026-04-28T01:00:01Z", "e1", "run tk start later"),
    ];
    const sections = run(events, new Map());
    const ung = sections[0].items.filter((i) => i.kind === "ungrouped");
    expect(ung).toHaveLength(1);
  });

  it("still applies a transition when the tk command is not at the command's front", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated s1 -> in_progress"),
      tkMsg("2026-04-28T01:00:02Z", "cd /code && tk close s1", "cc"),
      result("2026-04-28T01:00:02Z", "cc", "Updated s1 -> closed"),
    ];
    const sections = run(events, enrich({ s1: { status: "closed" } }));
    expect(stepItems(sections[0].items)[0].status).toBe("done");
  });

  // FIX A-VioB: real work batched in the SAME assistant message as a tk close
  // must stay inside the step, not fall out into an ungrouped run.
  it("keeps work batched with tk close in the same message inside the step", () => {
    const mixed: TranscriptEvent = {
      timestamp: "2026-04-28T01:00:03Z",
      type: "assistant_message",
      event_id: "a-mixed",
      source: "test",
      model: "m",
      text: "",
      tool_calls: [
        { tool_call_id: "real1", tool_name: "Edit", input_preview: `{"path":"x"}` },
        { tool_call_id: "tkc", tool_name: "Bash", input_preview: `{"command":"tk close s1"}` },
      ],
      stop_reason: null,
      usage: null,
      is_auth_error: false,
    };
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated s1 -> in_progress"),
      mixed,
      result("2026-04-28T01:00:03Z", "real1", "ok"),
      result("2026-04-28T01:00:03Z", "tkc", "Updated s1 -> closed"),
    ];
    const sections = run(events, enrich({ s1: { status: "closed" } }));
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].status).toBe("done");
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-mixed"]);
    expect(sections[0].items.filter((i) => i.kind === "ungrouped")).toHaveLength(0);
  });

  // FIX A-VioA: a step started again after being closed is active again.
  it("re-activates a step started again after being closed", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      tkMsg("2026-04-28T01:00:03Z", "tk close s1", "k2"),
      result("2026-04-28T01:00:03Z", "k2", "Updated s1 -> closed"),
      tkMsg("2026-04-28T01:00:04Z", "tk start s1", "k3"),
      result("2026-04-28T01:00:04Z", "k3", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:05Z", "Edit", "w2"),
      result("2026-04-28T01:00:05Z", "w2", "ok"),
    ];
    const sections = run(events, enrich({ s1: { status: "in_progress" } }), /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].status).toBe("active");
    expect(steps[0].is_frontier).toBe(true);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1", "a-w2"]);
  });

  // FIX B: a stop-hook chip between two reply fragments keeps chronological
  // order -- the pre-chip reply stays inline, only the post-chip reply trails.
  it("weaves a stop-hook chip between two reply fragments in order", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      tkMsg("2026-04-28T01:00:03Z", "tk close s1", "k2"),
      result("2026-04-28T01:00:03Z", "k2", "Updated s1 -> closed"),
      assistantText("2026-04-28T01:00:04Z", "reply A", "rA"),
      userMsg("2026-04-28T01:00:05Z", "Stop hook feedback:\nhook", "sh1"),
      assistantText("2026-04-28T01:00:06Z", "reply B", "rB"),
    ];
    const sections = run(events, enrich({ s1: { status: "closed" } }));
    expect(sections[0].items.map((i) => i.kind)).toEqual(["step", "ungrouped", "chip"]);
    const ung = sections[0].items.find((i) => i.kind === "ungrouped") as { events: { event_id: string }[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["rA"]);
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["rB"]);
  });
});

describe("regular ticket transitions (cod-oglc repro)", () => {
  // The real bababa case: while step s1 was open, the crystallize flow ran a
  // Bash command that created AND started a *regular* ticket (cod-oglc, no
  // `step: true`), so its output carried `Updated cod-oglc -> in_progress`.
  // A regular ticket is absent from the steps-only enrichment table, so it must
  // NOT appear as a timeline node -- it previously leaked in titled with its raw
  // id because enrichment had no entry to override the fallback title.
  it("does not render a started regular ticket as a step node", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated s1 -> in_progress"),
      // The crystallize command: not a recognised pure-tk call (begins with cd),
      // so it renders as work; its output starts a regular ticket.
      tkMsg("2026-04-28T01:00:02Z", "cd /code && tk create x && tk start cod-oglc", "cr"),
      result("2026-04-28T01:00:02Z", "cr", "Updated cod-oglc -> in_progress\nTICKET=cod-oglc"),
      workMsg("2026-04-28T01:00:03Z", "Bash", "w1"),
      result("2026-04-28T01:00:03Z", "w1", "ok"),
    ];
    // Enrichment knows s1 (a step) but NOT cod-oglc (a regular ticket).
    const sections = run(events, enrich({ s1: { status: "in_progress" } }), /* idle */ false);
    const steps = stepItems(sections[0].items);
    // Only the real step renders; cod-oglc never becomes a node.
    expect(steps.map((s) => s.ticket_id)).toEqual(["s1"]);
    // The crystallize command and the work after it stay inside the open step,
    // since the regular-ticket start never hijacked the current step.
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-cr", "a-w1"]);
  });
});

describe("batched transitions (bababa real transcript)", () => {
  // The real bababa case: the agent batched `tk close nzb4 && tk close p5jc &&
  // tk start ts53` into ONE Bash command, so all three transitions arrive in
  // one tool output. Node order must still follow transition order
  // (nzb4, p5jc, ts53) -- the started step must not hoist above the
  // closed-without-work step.
  it("orders a batched close+close+start by transition order, not opens-first", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "set up my inbox view"),
      tkMsg("2026-04-28T01:00:01Z", "tk start cod-nzb4", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated cod-nzb4 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Bash", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      tkMsg("2026-04-28T01:00:03Z", "tk close cod-nzb4 && tk close cod-p5jc && tk start cod-ts53", "kb"),
      result(
        "2026-04-28T01:00:03Z",
        "kb",
        "Updated cod-nzb4 -> closed\nUpdated cod-p5jc -> closed\nUpdated cod-ts53 -> in_progress",
      ),
      assistantText("2026-04-28T01:00:04Z", "Now let me fetch a sample.", "narr"),
      workMsg("2026-04-28T01:00:05Z", "Bash", "w2"),
      result("2026-04-28T01:00:05Z", "w2", "ok"),
    ];
    const sections = run(
      events,
      enrich({
        "cod-nzb4": { status: "closed" },
        "cod-p5jc": { status: "closed" },
        "cod-ts53": { status: "in_progress" },
      }),
      /* idle */ false,
    );
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["cod-nzb4", "cod-p5jc", "cod-ts53"]);
  });
});

describe("missing tickets directory (step-id fallback)", () => {
  // When the user clears the .tickets/ directory, the enrichment table goes
  // empty. A step id minted by `tk create --step` carries a `-step-` segment
  // (e.g. cod-step-aaaa), so the walk still recognises it as a step from the
  // transition line alone -- the grouping survives, titled with the raw id and
  // flagged file_missing. A regular ticket id has no marker and stays filtered.

  it("keeps a step's grouping when its file is gone (empty enrichment)", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start cod-step-aaaa", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated cod-step-aaaa -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
      tkMsg("2026-04-28T01:00:03Z", "tk close cod-step-aaaa 'did it'", "k2"),
      result("2026-04-28T01:00:03Z", "k2", "Updated cod-step-aaaa -> closed"),
    ];
    // No enrichment at all -- the directory was deleted.
    const sections = run(events, new Map());
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].ticket_id).toBe("cod-step-aaaa");
    expect(steps[0].status).toBe("done");
    // Title falls back to the raw id; the node is flagged as file-missing.
    expect(steps[0].title).toBe("cod-step-aaaa");
    expect(steps[0].file_missing).toBe(true);
    // The grouped work is still attributed to the step.
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1"]);
  });

  it("still filters a picked-up regular ticket when enrichment is empty", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start cod-step-aaaa", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated cod-step-aaaa -> in_progress"),
      // A regular ticket the agent picked up: not a recognised pure-tk call
      // (begins with cd), so it renders as work; its output starts the ticket.
      tkMsg("2026-04-28T01:00:02Z", "cd /code && tk start cod-oglc", "cr"),
      result("2026-04-28T01:00:02Z", "cr", "Updated cod-oglc -> in_progress"),
      workMsg("2026-04-28T01:00:03Z", "Bash", "w1"),
      result("2026-04-28T01:00:03Z", "w1", "ok"),
    ];
    const sections = run(events, new Map(), /* idle */ false);
    const steps = stepItems(sections[0].items);
    // Only the step-shaped id becomes a node; the regular ticket is skipped
    // even though enrichment is empty, and its command stays inside the step.
    expect(steps.map((s) => s.ticket_id)).toEqual(["cod-step-aaaa"]);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-cr", "a-w1"]);
  });

  it("does not flag a step as file-missing when enrichment still has it", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start cod-step-aaaa", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated cod-step-aaaa -> in_progress"),
    ];
    const sections = run(events, enrich({ "cod-step-aaaa": { title: "Do the thing" } }), /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    // File present: enriched title wins and the node is not flagged.
    expect(steps[0].title).toBe("Do the thing");
    expect(steps[0].file_missing).toBe(false);
  });

  it("drops an old marker-less step id once its file is gone (accepted limitation)", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "go"),
      tkMsg("2026-04-28T01:00:01Z", "tk start s1", "k1"),
      result("2026-04-28T01:00:01Z", "k1", "Updated s1 -> in_progress"),
      workMsg("2026-04-28T01:00:02Z", "Edit", "w1"),
      result("2026-04-28T01:00:02Z", "w1", "ok"),
    ];
    // An old-format step id (no `-step-`) with no enrichment cannot be told
    // apart from a regular ticket, so it is skipped -- the work renders inline.
    const sections = run(events, new Map(), /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(0);
    expect(sections[0].items.some((i) => i.kind === "ungrouped")).toBe(true);
  });
});

describe("inline system notifications (background tasks)", () => {
  const TASK_NOTIF =
    "<task-notification>\n<status>completed</status>\n" +
    '<summary>Background command "poll worker" completed (exit code 0)</summary>\n</task-notification>';

  it("weaves a background-task notification into the current turn instead of splitting it", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "kick off the poll", "go"),
      workMsg("2026-04-28T01:00:01Z", "Bash", "w1"),
      result("2026-04-28T01:00:01Z", "w1", "launched"),
      // The background command finishes: re-invokes the agent mid-flow.
      userMsg("2026-04-28T01:00:02Z", TASK_NOTIF, "tn1"),
      workMsg("2026-04-28T01:00:03Z", "Edit", "w2"),
      result("2026-04-28T01:00:03Z", "w2", "ok"),
      assistantText("2026-04-28T01:00:04Z", "all done", "rep"),
    ];
    const sections = run(events);
    // The notification did NOT open a new turn.
    expect(sections).toHaveLength(1);
    // It renders as a chip, and the follow-up work stays in the same section.
    const chip = sections[0].items.find((i) => i.kind === "chip") as { event: { event_id: string } } | undefined;
    expect(chip?.event.event_id).toBe("tn1");
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["rep"]);
  });

  it("shows a notification that arrives before any human turn in a user-less section", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", TASK_NOTIF, "tn1"),
      assistantText("2026-04-28T01:00:01Z", "picking back up", "rep"),
    ];
    const sections = run(events);
    expect(sections).toHaveLength(1);
    expect(sections[0].user_event).toBeNull();
    const chip = sections[0].items.find((i) => i.kind === "chip") as { event: { event_id: string } } | undefined;
    expect(chip?.event.event_id).toBe("tn1");
  });
});
