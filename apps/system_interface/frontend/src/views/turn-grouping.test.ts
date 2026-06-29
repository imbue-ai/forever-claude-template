import { describe, expect, it } from "vitest";
import type { TranscriptEvent, ToolResultEvent, AssistantMessageEvent, UserMessageEvent } from "../models/Response";
import type { StepNode, TimelineItem } from "./turn-grouping";
import { buildSections } from "./turn-grouping";
import type { PermissionResolution } from "./message-classification";

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
    tool_calls: [{ tool_call_id: callId, tool_name: "Bash", input_preview: JSON.stringify({ command }) }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

/** A permission-request message: a Bash latchkey POST to the reserved host,
 *  optionally carrying explanatory prose alongside the call. */
function permissionMsg(ts: string, callId: string, text = "", id = `a-${callId}`): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: id,
    source: "test",
    model: "m",
    text,
    tool_calls: [
      {
        tool_call_id: callId,
        tool_name: "Bash",
        input_preview: JSON.stringify({
          command: "latchkey curl -XPOST http://latchkey-self.invalid/permission-requests -d '{}'",
        }),
      },
    ],
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

// --- tk stdout decoration helpers (the lines tk prints; see vendor/tk/ticket) ---

/** `tk start` output: the transition line plus, for a step, its title line. */
function startOut(id: string, title?: string): string {
  const base = `Updated ${id} -> in_progress`;
  return title === undefined ? base : `${base}\ntk-step ${id} title: ${title}`;
}

/** `tk close` output: the transition line plus, for a step, its title + summary. */
function closeOut(id: string, title?: string, summary?: string): string {
  let out = `Updated ${id} -> closed`;
  if (title !== undefined) out += `\ntk-step ${id} title: ${title}`;
  if (summary !== undefined) out += `\ntk-step ${id} summary: ${summary}`;
  return out;
}

/** Build the toolResults map from the event list (as ChatPanel does) and run.
 *  No enrichment argument -- structure and decoration both come from the walk. */
function run(events: TranscriptEvent[], agentIsIdle = true) {
  const toolResults = new Map<string, ToolResultEvent>();
  for (const e of events) {
    if (e.type === "tool_result") toolResults.set(e.tool_call_id, e);
  }
  return buildSections(events, toolResults, agentIsIdle);
}

function stepItems(items: TimelineItem[]): StepNode[] {
  return items.filter((i): i is { kind: "step"; step: StepNode } => i.kind === "step").map((i) => i.step);
}

describe("bug fixes", () => {
  // Work done before the first step must not vanish.
  it("renders tool calls done before the first step in an ungrouped item", () => {
    const events = [
      userMsg("t0", "go"),
      workMsg("t1", "Read", "tc-read"),
      result("t2", "tc-read", "file contents"),
      tkMsg("t3", "tk start s1", "tc-start"),
      result("t4", "tc-start", startOut("s1", "Do it")),
      workMsg("t5", "Edit", "tc-edit"),
      result("t6", "tc-edit", "ok"),
    ];
    const sections = run(events);
    expect(sections).toHaveLength(1);
    const items = sections[0].items;
    expect(items[0].kind).toBe("ungrouped");
    const ung = items[0] as { kind: "ungrouped"; events: AssistantMessageEvent[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["a-tc-read"]);
    expect(items[1].kind).toBe("step");
    const step = (items[1] as { kind: "step"; step: StepNode }).step;
    expect(step.ticket_id).toBe("s1");
    expect(step.title).toBe("Do it");
    expect(step.events.map((e) => e.event_id)).toEqual(["a-tc-edit"]);
  });

  // A started step renders in its transcript position, never hoisted up.
  it("positions an in-progress step after earlier closed steps, not at the top", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start a", "t-a-start"),
      result("t1", "t-a-start", startOut("a", "A")),
      workMsg("t2", "Edit", "w-a"),
      result("t2", "w-a", "ok"),
      tkMsg("t3", "tk close a", "t-a-close"),
      result("t3", "t-a-close", closeOut("a", "A", "did a")),
      tkMsg("t4", "tk start b", "t-b-start"),
      result("t4", "t-b-start", startOut("b", "B")),
      workMsg("t5", "Edit", "w-b"),
      result("t5", "w-b", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["a", "b"]);
    expect(steps[0].status).toBe("done");
    expect(steps[1].status).toBe("active");
    expect(steps[1].is_frontier).toBe(true);
  });
});

describe("step ordering edge cases", () => {
  // A step closed without ever being started -- and thus with no work events --
  // must still render at its transcript position, not be shoved below a later
  // step that does have work. p5jc closes (no work) before ts53 starts.
  it("positions a no-work step at its transition spot, not below a later working step", () => {
    const events = [
      userMsg("t0", "set up my inbox view"),
      tkMsg("t1", "tk start nzb4", "k1"),
      result("t1", "k1", startOut("nzb4", "First")),
      workMsg("t2", "Bash", "w1"),
      result("t2", "w1", "ok"),
      tkMsg("t3", "tk close nzb4", "k2"),
      result("t3", "k2", closeOut("nzb4", "First", "did first")),
      // p5jc is closed directly, with no start and no work.
      tkMsg("t4", "tk close p5jc", "k3"),
      result("t4", "k3", closeOut("p5jc", "Second", "did second")),
      tkMsg("t5", "tk start ts53", "k4"),
      result("t5", "k4", startOut("ts53", "Third")),
      assistantText("t6", "Now let me fetch a sample.", "narr"),
      workMsg("t7", "Bash", "w2"),
      result("t7", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["nzb4", "p5jc", "ts53"]);
    expect(steps[1].status).toBe("done"); // p5jc: done, in the middle
    expect(steps[2].status).toBe("active"); // ts53: active, at the bottom
  });
});

describe("decoration from the transcript", () => {
  it("groups a step's work and shows its title and close summary from tk stdout", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Fix it")),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      tkMsg("t3", "tk close s1", "t2"),
      result("t3", "t2", closeOut("s1", "Fix it", "Fixed the bug")),
    ];
    const sections = run(events);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].title).toBe("Fix it");
    expect(steps[0].status).toBe("done");
    expect(steps[0].summary).toBe("Fixed the bug");
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1"]);
  });

  it("reads a pending step's title from its `Created` line", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk create --step 'Look around'\ntk create --step 'Then fix'", "tc"),
      result("t1", "tc", "Created cod-step-aaaa: Look around\nCreated cod-step-bbbb: Then fix"),
      tkMsg("t2", "tk start cod-step-aaaa", "s1"),
      result("t2", "s1", startOut("cod-step-aaaa", "Look around")),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    // The started step, then the never-started one as a pending placeholder.
    expect(steps.map((s) => s.ticket_id)).toEqual(["cod-step-aaaa", "cod-step-bbbb"]);
    expect(steps[0].status).toBe("active");
    expect(steps[1].status).toBe("pending");
    expect(steps[1].title).toBe("Then fix");
  });

  it("does not render tk lifecycle commands as work", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Do it")),
    ];
    const sections = run(events);
    const steps = stepItems(sections[0].items);
    expect(steps[0].events).toHaveLength(0);
    expect(sections[0].items.filter((i) => i.kind === "ungrouped")).toHaveLength(0);
  });
});

describe("historical input fallback", () => {
  // Pre-redesign transcripts predate the tk stdout decoration lines. Titles live
  // in the batched `S1=$(tk create --step "...")` command input (the id was
  // captured into a shell var and only the var=id echo reaches the output);
  // summaries live in the `tk close <id> "summary"` input. The fallback recovers
  // both -- titles by zipping the create titles onto the echoed ids in order.
  it("recovers titles (batched create) and a close summary from old-format inputs", () => {
    const batchedCreate =
      'S1=$(tk create --step "Locate the folder")\n' +
      'S2=$(tk create --step "Read the docs")\n' +
      'echo "S1=$S1"; echo "S2=$S2"';
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", batchedCreate, "tc"),
      result("t1", "tc", "S1=cod-step-6xo5\nS2=cod-step-uazv"),
      tkMsg("t2", "tk start cod-step-6xo5", "s1"),
      result("t2", "s1", "Updated cod-step-6xo5 -> in_progress"),
      workMsg("t3", "Edit", "w1"),
      result("t3", "w1", "ok"),
      tkMsg("t4", 'tk close cod-step-6xo5 "Found and read it all."', "c1"),
      result("t4", "c1", "Updated cod-step-6xo5 -> closed"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    const located = steps.find((s) => s.ticket_id === "cod-step-6xo5")!;
    expect(located.title).toBe("Locate the folder"); // recovered from create input
    expect(located.status).toBe("done");
    expect(located.summary).toBe("Found and read it all."); // recovered from close input
    // The never-started second step shows as a pending placeholder, titled.
    const pending = steps.find((s) => s.ticket_id === "cod-step-uazv")!;
    expect(pending.status).toBe("pending");
    expect(pending.title).toBe("Read the docs");
  });
});

describe("narration and close-time ejection", () => {
  it("keeps mid-work narration in the step, not as the reply", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Do it")),
      assistantText("t2", "Found it, patching now.", "narr"),
      workMsg("t3", "Edit", "w1"),
      result("t3", "w1", "ok"),
      assistantText("t4", "Done.", "reply"),
    ];
    const sections = run(events);
    const steps = stepItems(sections[0].items);
    expect(steps[0].narration).toBe("Found it, patching now.");
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["reply"]);
  });

  // The live moment after the agent starts a step and speaks, but before it has
  // issued any tool call: the prose is the step's in-flight narration (a caption
  // under the still-spinning step), NOT the below-timeline wrap-up reply.
  it("shows a live step's just-spoken prose as narration before any tool call", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Do it")),
      assistantText("t2", "Looking into it now.", "narr"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].is_frontier).toBe(true);
    expect(steps[0].narration).toBe("Looking into it now.");
    // Not ejected into an inline run, and not the below-timeline reply.
    expect(sections[0].items.filter((i) => i.kind === "ungrouped")).toHaveLength(0);
    expect(sections[0].trailing_reply).toHaveLength(0);
  });

  // Same live step, after it has done work and then spoken again: the caption
  // tracks the LATEST line, not the earlier prose that happened to precede work.
  it("updates a live step's narration to the latest prose after its work", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Do it")),
      assistantText("t2", "Patching now.", "narr1"),
      workMsg("t3", "Edit", "w1"),
      result("t3", "w1", "ok"),
      assistantText("t4", "Checking the result.", "narr2"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps[0].is_frontier).toBe(true);
    expect(steps[0].narration).toBe("Checking the result.");
    expect(sections[0].trailing_reply).toHaveLength(0);
  });

  // When the agent goes idle with a step still open, its trailing prose is the
  // wrap-up reply (no spinner, so no in-flight narration to preserve).
  it("treats trailing prose of an idle open step as the below-timeline reply", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Do it")),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      assistantText("t3", "All done.", "reply"),
    ];
    const sections = run(events, /* idle */ true);
    const steps = stepItems(sections[0].items);
    expect(steps[0].is_frontier).toBe(false);
    expect(steps[0].narration).toBeNull();
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["reply"]);
  });

  it("treats prose before the first step as an ungrouped (leading) item", () => {
    const events = [
      userMsg("t0", "go"),
      assistantText("t1", "Sure, tracing the auth path.", "lead"),
      tkMsg("t2", "tk start s1", "t1"),
      result("t2", "t1", startOut("s1", "Trace")),
      workMsg("t3", "Edit", "w1"),
      result("t3", "w1", "ok"),
    ];
    const sections = run(events);
    const items = sections[0].items;
    expect(items[0].kind).toBe("ungrouped");
    const ung = items[0] as { kind: "ungrouped"; events: AssistantMessageEvent[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["lead"]);
    expect(sections[0].trailing_reply).toHaveLength(0);
  });

  // A step's closing prose (after its last work) is ejected into the inline
  // stream right after the step node -- it is NOT buried in the step and NOT the
  // below-timeline reply when more steps follow.
  it("ejects a step's closing prose to an ungrouped block between the two steps", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "First")),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      assistantText("t3", "Found the theme; wiring it up next.", "mid"),
      tkMsg("t4", "tk close s1", "t1c"),
      result("t4", "t1c", closeOut("s1", "First", "did first")),
      tkMsg("t5", "tk start s2", "t2"),
      result("t5", "t2", startOut("s2", "Second")),
      workMsg("t6", "Edit", "w2"),
      result("t6", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const items = sections[0].items;
    expect(items.map((i) => i.kind)).toEqual(["step", "ungrouped", "step"]);
    const ung = items[1] as { kind: "ungrouped"; events: AssistantMessageEvent[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["mid"]);
    const steps = stepItems(items);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1"]); // not buried
    expect(steps[0].narration).toBeNull();
    expect(sections[0].trailing_reply).toHaveLength(0);
  });

  // The live moment between "start the next step" and "issue its first tool
  // call": the closing prose is already ejected, not the below-timeline reply.
  it("ejects closing prose the moment the next step starts, before it does work", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "First")),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      assistantText("t3", "Done with that; on to the next.", "mid"),
      tkMsg("t4", "tk close s1", "t1c"),
      result("t4", "t1c", closeOut("s1", "First", "did first")),
      tkMsg("t5", "tk start s2", "t2"),
      result("t5", "t2", startOut("s2", "Second")),
    ];
    const sections = run(events, /* idle */ false);
    const items = sections[0].items;
    expect(items.map((i) => i.kind)).toEqual(["step", "ungrouped", "step"]);
    const ung = items[1] as { kind: "ungrouped"; events: AssistantMessageEvent[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["mid"]);
    expect(sections[0].trailing_reply).toHaveLength(0);
    const steps = stepItems(items);
    expect(steps[1].is_frontier).toBe(true);
  });

  // Closing prose with nothing after it (the last step of the turn) is the
  // below-timeline reply, not an inline ejected block.
  it("keeps closing prose of the last step as the below-timeline reply", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "First")),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      assistantText("t3", "All done -- anything else?", "reply"),
      tkMsg("t4", "tk close s1", "t1c"),
      result("t4", "t1c", closeOut("s1", "First", "did first")),
    ];
    const sections = run(events);
    expect(sections[0].items.every((i) => i.kind !== "ungrouped")).toBe(true);
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["reply"]);
  });

  // Narration (prose followed by more work in the SAME step) stays in the step;
  // only the prose after the step's last work is ejected.
  it("keeps mid-step narration in the step while ejecting only the closing prose", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "First")),
      assistantText("t2", "Patching now.", "narr"),
      workMsg("t3", "Edit", "w1"),
      result("t3", "w1", "ok"),
      assistantText("t4", "Patched; moving on.", "close"),
      tkMsg("t5", "tk close s1", "t1c"),
      result("t5", "t1c", closeOut("s1", "First", "did first")),
      tkMsg("t6", "tk start s2", "t2"),
      result("t6", "t2", startOut("s2", "Second")),
      workMsg("t7", "Edit", "w2"),
      result("t7", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps[0].narration).toBe("Patching now.");
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["narr", "a-w1"]);
    const ung = sections[0].items.find((i) => i.kind === "ungrouped") as { events: AssistantMessageEvent[] };
    expect(ung.events.map((e) => e.event_id)).toEqual(["close"]);
  });
});

describe("carryover", () => {
  it("re-renders a still-open step at the top of the next turn with frozen prior state", () => {
    const events = [
      userMsg("t0", "first", "u1"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Do it")),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      userMsg("t10", "second", "u2"),
      workMsg("t11", "Edit", "w2"),
      result("t11", "w2", "ok"),
      tkMsg("t12", "tk close s1", "t2"),
      result("t12", "t2", closeOut("s1", "Do it", "did it")),
    ];
    const sections = run(events);
    expect(sections).toHaveLength(2);

    const s1FirstTurn = stepItems(sections[0].items)[0];
    expect(s1FirstTurn.is_carryover).toBe(false);
    expect(s1FirstTurn.status).toBe("active"); // frozen: never flips to done here
    expect(s1FirstTurn.title).toBe("Do it"); // title resolved from the global decoration map
    expect(s1FirstTurn.events.map((e) => e.event_id)).toEqual(["a-w1"]);

    const s1SecondTurn = stepItems(sections[1].items)[0];
    expect(sections[1].items[0].kind).toBe("step"); // at the top
    expect(s1SecondTurn.is_carryover).toBe(true);
    expect(s1SecondTurn.status).toBe("done");
    expect(s1SecondTurn.summary).toBe("did it");
    expect(s1SecondTurn.events.map((e) => e.event_id)).toEqual(["a-w2"]);
  });

  // A user message arriving mid-work (no stop hook) is just another boundary:
  // the open step carries over via the open-stack, with no auto-close involved.
  it("carries a step over a mid-work user message", () => {
    const events = [
      userMsg("t0", "first", "u1"),
      tkMsg("t1", "tk start s1", "t1"),
      result("t1", "t1", startOut("s1", "Do it")),
      userMsg("t5", "actually also check X", "u2"),
      workMsg("t6", "Edit", "w2"),
      result("t6", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    expect(sections).toHaveLength(2);
    expect(stepItems(sections[1].items)[0].is_carryover).toBe(true);
    expect(stepItems(sections[1].items)[0].events.map((e) => e.event_id)).toEqual(["a-w2"]);
  });
});

describe("pending roster", () => {
  it("appends never-started steps as pending placeholders at the tail, in transcript order", () => {
    const events = [
      userMsg("t0", "go"),
      // Batched create declares three steps up front, in this order.
      tkMsg("t1", "tk create --step 'One'\ntk create --step 'Two'\ntk create --step 'Three'", "tc"),
      result("t1", "tc", "Created cod-step-1: One\nCreated cod-step-2: Two\nCreated cod-step-3: Three"),
      tkMsg("t2", "tk start cod-step-1", "t1"),
      result("t2", "t1", startOut("cod-step-1", "One")),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    // cod-step-1 active first; then cod-step-2, cod-step-3 pending in creation order.
    expect(steps.map((s) => s.ticket_id)).toEqual(["cod-step-1", "cod-step-2", "cod-step-3"]);
    expect(steps[1].status).toBe("pending");
    expect(steps[1].title).toBe("Two");
    expect(steps[2].status).toBe("pending");
    expect(steps[0].is_frontier).toBe(true);
  });

  it("does not show pending placeholders in a non-tail section", () => {
    const events = [
      userMsg("t0", "first", "u1"),
      tkMsg("t1", "tk create --step 'Main'\ntk create --step 'Later'", "tc"),
      result("t1", "tc", "Created cod-step-main: Main\nCreated cod-step-later: Later"),
      tkMsg("t2", "tk start cod-step-main", "t1"),
      result("t2", "t1", startOut("cod-step-main", "Main")),
      tkMsg("t3", "tk close cod-step-main", "t1c"),
      result("t3", "t1c", closeOut("cod-step-main", "Main", "did main")),
      userMsg("t10", "second", "u2"),
    ];
    const sections = run(events);
    // The pending step shows only in the tail (second) section.
    expect(stepItems(sections[0].items).map((s) => s.ticket_id)).toEqual(["cod-step-main"]);
    expect(stepItems(sections[1].items).map((s) => s.ticket_id)).toEqual(["cod-step-later"]);
  });
});

describe("audit regressions", () => {
  // A Bash command that merely mentions a tk verb must render as work, not be
  // misclassified as a tk lifecycle command and silently dropped.
  it("renders a non-tk command that mentions a tk verb as work (grouped under the open step)", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "k1"),
      result("t1", "k1", startOut("s1", "Do it")),
      // A real git command whose message mentions "tk close" -- not a tk command.
      tkMsg("t2", "git commit -m 'tk close the bug'", "gc"),
      result("t2", "gc", "[main abc] tk close the bug"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-gc"]);
  });

  it("keeps a tk-mentioning command visible (ungrouped) when no step is open", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "echo 'run tk start later'", "e1"),
      result("t1", "e1", "run tk start later"),
    ];
    const sections = run(events);
    const ung = sections[0].items.filter((i) => i.kind === "ungrouped");
    expect(ung).toHaveLength(1);
  });

  it("still applies a transition when the tk command is not at the command's front", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start cod-step-s1", "k1"),
      result("t1", "k1", startOut("cod-step-s1", "Do it")),
      tkMsg("t2", "cd /code && tk close cod-step-s1", "cc"),
      result("t2", "cc", closeOut("cod-step-s1", "Do it", "did it")),
    ];
    const sections = run(events);
    expect(stepItems(sections[0].items)[0].status).toBe("done");
  });

  // Real work batched in the SAME assistant message as a tk close must stay
  // inside the step, not fall out into an ungrouped run.
  it("keeps work batched with tk close in the same message inside the step", () => {
    const mixed: TranscriptEvent = {
      timestamp: "t3",
      type: "assistant_message",
      event_id: "a-mixed",
      source: "test",
      model: "m",
      text: "",
      tool_calls: [
        { tool_call_id: "real1", tool_name: "Edit", input_preview: `{"path":"x"}` },
        { tool_call_id: "tkc", tool_name: "Bash", input_preview: JSON.stringify({ command: "tk close s1" }) },
      ],
      stop_reason: null,
      usage: null,
      is_auth_error: false,
    };
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "k1"),
      result("t1", "k1", startOut("s1", "Do it")),
      mixed,
      result("t3", "real1", "ok"),
      result("t3", "tkc", closeOut("s1", "Do it", "did it")),
    ];
    const sections = run(events);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].status).toBe("done");
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-mixed"]);
    expect(sections[0].items.filter((i) => i.kind === "ungrouped")).toHaveLength(0);
  });

  // A step started again after being closed is active again.
  it("re-activates a step started again after being closed", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "k1"),
      result("t1", "k1", startOut("s1", "Do it")),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      tkMsg("t3", "tk close s1", "k2"),
      result("t3", "k2", closeOut("s1", "Do it", "did it")),
      tkMsg("t4", "tk start s1", "k3"),
      result("t4", "k3", startOut("s1", "Do it")),
      workMsg("t5", "Edit", "w2"),
      result("t5", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].status).toBe("active");
    expect(steps[0].is_frontier).toBe(true);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1", "a-w2"]);
  });

  // A stop-hook chip renders at its chronological position in the timeline.
  // Chips are no longer reply boundaries: the wrap-up reply is the final run of
  // ungrouped prose regardless of where a chip fell.
  it("renders a stop-hook chip at its position with the reply below the timeline", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "k1"),
      result("t1", "k1", startOut("s1", "Do it")),
      userMsg("t2", "Stop hook feedback:\nhook", "sh1"),
      workMsg("t3", "Edit", "w1"),
      result("t3", "w1", "ok"),
      tkMsg("t4", "tk close s1", "k2"),
      result("t4", "k2", closeOut("s1", "Do it", "did it")),
      assistantText("t5", "All wrapped up.", "reply"),
    ];
    const sections = run(events);
    expect(sections[0].items.some((i) => i.kind === "chip")).toBe(true);
    expect(sections[0].trailing_reply.map((e) => e.event_id)).toEqual(["reply"]);
  });
});

describe("regular ticket transitions", () => {
  // While step s1 was open, a Bash command created AND started a *regular*
  // ticket (cod-oglc, no `-step-` id), so its output carried
  // `Updated cod-oglc -> in_progress`. A regular ticket must NOT appear as a
  // timeline node -- it is neither a `-step-` id nor a known step.
  it("does not render a started regular ticket as a step node", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start cod-step-s1", "k1"),
      result("t1", "k1", startOut("cod-step-s1", "Do it")),
      // The crystallize command: not a recognised pure-tk call (begins with cd),
      // so it renders as work; its output starts a regular ticket.
      tkMsg("t2", "cd /code && tk create x && tk start cod-oglc", "cr"),
      result("t2", "cr", "Updated cod-oglc -> in_progress\nTICKET=cod-oglc"),
      workMsg("t3", "Bash", "w1"),
      result("t3", "w1", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["cod-step-s1"]);
    // The command and the work after it stay inside the open step.
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-cr", "a-w1"]);
  });
});

describe("batched transitions (one command, several transitions)", () => {
  // The agent batched `tk close nzb4 && tk close p5jc && tk start ts53` into ONE
  // Bash command, so all three transitions arrive in one tool output. Node order
  // must follow transition order, not opens-first.
  it("orders a batched close+close+start by transition order, not opens-first", () => {
    const events = [
      userMsg("t0", "set up my inbox view"),
      tkMsg("t1", "tk start cod-step-nzb4", "k1"),
      result("t1", "k1", startOut("cod-step-nzb4", "First")),
      workMsg("t2", "Bash", "w1"),
      result("t2", "w1", "ok"),
      tkMsg("t3", "tk close cod-step-nzb4 && tk close cod-step-p5jc && tk start cod-step-ts53", "kb"),
      result(
        "t3",
        "kb",
        [
          closeOut("cod-step-nzb4", "First", "did first"),
          closeOut("cod-step-p5jc", "Second", "did second"),
          startOut("cod-step-ts53", "Third"),
        ].join("\n"),
      ),
      assistantText("t4", "Now let me fetch a sample.", "narr"),
      workMsg("t5", "Bash", "w2"),
      result("t5", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["cod-step-nzb4", "cod-step-p5jc", "cod-step-ts53"]);
  });
});

describe("step-id fallback (no decoration in the loaded window)", () => {
  // A step id minted by `tk create --step` carries a `-step-` segment, so the
  // walk recognises it as a step from the transition line alone even when its
  // create/decoration lines scrolled out of the window. The title falls back to
  // the raw id; there is no summary.
  it("keeps a step's grouping when its decoration is absent", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start cod-step-aaaa", "k1"),
      result("t1", "k1", "Updated cod-step-aaaa -> in_progress"),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
      tkMsg("t3", "tk close cod-step-aaaa 'did it'", "k2"),
      result("t3", "k2", "Updated cod-step-aaaa -> closed"),
    ];
    const sections = run(events);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(1);
    expect(steps[0].ticket_id).toBe("cod-step-aaaa");
    expect(steps[0].status).toBe("done");
    // Title falls back to the raw id; the close summary IS recovered from the
    // close command input (id + summary both present there).
    expect(steps[0].title).toBe("cod-step-aaaa");
    expect(steps[0].summary).toBe("did it");
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-w1"]);
  });

  it("still filters a picked-up regular ticket with no decoration", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start cod-step-aaaa", "k1"),
      result("t1", "k1", "Updated cod-step-aaaa -> in_progress"),
      // A regular ticket the agent picked up: renders as work; output starts it.
      tkMsg("t2", "cd /code && tk start cod-oglc", "cr"),
      result("t2", "cr", "Updated cod-oglc -> in_progress"),
      workMsg("t3", "Bash", "w1"),
      result("t3", "w1", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps.map((s) => s.ticket_id)).toEqual(["cod-step-aaaa"]);
    expect(steps[0].events.map((e) => e.event_id)).toEqual(["a-cr", "a-w1"]);
  });

  it("drops an old marker-less step id with no decoration (accepted limitation)", () => {
    const events = [
      userMsg("t0", "go"),
      tkMsg("t1", "tk start s1", "k1"),
      result("t1", "k1", "Updated s1 -> in_progress"),
      workMsg("t2", "Edit", "w1"),
      result("t2", "w1", "ok"),
    ];
    // An old-format step id (no `-step-`) with no decoration cannot be told
    // apart from a regular ticket, so it is skipped -- the work renders inline.
    const sections = run(events, /* idle */ false);
    const steps = stepItems(sections[0].items);
    expect(steps).toHaveLength(0);
    expect(sections[0].items.some((i) => i.kind === "ungrouped")).toBe(true);
  });
});

describe("permission request breaks", () => {
  // A permission request must never be collapsed inside a step -- the user has
  // to see and act on it. It is lifted out into its own inline `permission`
  // item, and detection is input-only so it surfaces even while still pending.

  it("lifts a permission request out of the open step, even while pending", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      tkMsg("2026-05-01T01:00:01Z", "tk start s1", "c-s1"),
      result("2026-05-01T01:00:01Z", "c-s1", startOut("s1", "Do it")),
      workMsg("2026-05-01T01:00:02Z", "Edit", "w1"),
      result("2026-05-01T01:00:02Z", "w1", "ok"),
      // Permission request with NO tool result yet: still pending.
      permissionMsg("2026-05-01T01:00:03Z", "perm"),
      // Work resumes in the same step after the (modelled) approval.
      workMsg("2026-05-01T01:00:04Z", "Edit", "w2"),
      result("2026-05-01T01:00:04Z", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const items = sections[0].items;
    expect(items.map((i) => i.kind)).toEqual(["step", "permission"]);

    const step = (items[0] as { kind: "step"; step: StepNode }).step;
    // The step stays active and keeps BOTH the pre- and post-request work; the
    // permission message itself is not among its grouped events.
    expect(step.status).toBe("active");
    expect(step.events.map((e) => e.event_id)).toEqual(["a-w1", "a-w2"]);

    const perm = items[1] as { kind: "permission"; event: AssistantMessageEvent };
    expect(perm.event.event_id).toBe("a-perm");
  });

  it("renders a permission request with no open step as its own break", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      workMsg("2026-05-01T01:00:01Z", "Read", "w1"),
      result("2026-05-01T01:00:01Z", "w1", "ok"),
      permissionMsg("2026-05-01T01:00:02Z", "perm"),
      result("2026-05-01T01:00:02Z", "perm", '{"request_id":"abc"}'),
    ];
    const sections = run(events);
    const items = sections[0].items;
    // The pre-request work coalesces into an ungrouped run; the request stands
    // apart as its own permission break rather than merging into that run.
    expect(items.map((i) => i.kind)).toEqual(["ungrouped", "permission"]);
  });

  it("places the break between the step it interrupted and a later step", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      tkMsg("2026-05-01T01:00:01Z", "tk start s1", "c-s1"),
      result("2026-05-01T01:00:01Z", "c-s1", startOut("s1", "S1")),
      permissionMsg("2026-05-01T01:00:02Z", "perm"),
      result("2026-05-01T01:00:02Z", "perm", '{"request_id":"abc"}'),
      tkMsg("2026-05-01T01:00:03Z", "tk close s1", "x-s1"),
      result("2026-05-01T01:00:03Z", "x-s1", closeOut("s1", "S1")),
      tkMsg("2026-05-01T01:00:04Z", "tk start s2", "c-s2"),
      result("2026-05-01T01:00:04Z", "c-s2", startOut("s2", "S2")),
      workMsg("2026-05-01T01:00:05Z", "Edit", "w2"),
      result("2026-05-01T01:00:05Z", "w2", "ok"),
    ];
    const sections = run(events, /* idle */ false);
    const items = sections[0].items;
    expect(items.map((i) => i.kind)).toEqual(["step", "permission", "step"]);
    expect((items[0] as { kind: "step"; step: StepNode }).step.ticket_id).toBe("s1");
    expect((items[2] as { kind: "step"; step: StepNode }).step.ticket_id).toBe("s2");
  });

  it("keeps in-step prose before a trailing permission request out of the reply", () => {
    // Prose that precedes a trailing permission request is in-step narration,
    // not the wrap-up reply -- the request acts as the reply boundary, so the
    // prose stays in its step rather than being hoisted below the timeline.
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      tkMsg("2026-05-01T01:00:01Z", "tk start s1", "c-s1"),
      result("2026-05-01T01:00:01Z", "c-s1", startOut("s1", "Do it")),
      workMsg("2026-05-01T01:00:02Z", "Edit", "w1"),
      result("2026-05-01T01:00:02Z", "w1", "ok"),
      assistantText("2026-05-01T01:00:03Z", "About to do the risky bit."),
      permissionMsg("2026-05-01T01:00:04Z", "perm"),
      result("2026-05-01T01:00:04Z", "perm", '{"request_id":"abc"}'),
    ];
    const sections = run(events, /* idle */ false);
    expect(sections[0].trailing_reply).toHaveLength(0);
    expect(sections[0].items.map((i) => i.kind)).toEqual(["step", "permission"]);
  });
});

type PermissionItem = { kind: "permission"; resolution: PermissionResolution | null };

describe("permission resolutions", () => {
  // When the user grants/denies, the app injects a plain user message; the walk
  // reads its verdict onto the card and treats the notification as a turn
  // boundary (no user bubble) so an open step carries over the normal way.

  it("marks the card granted and opens a fresh turn with no user bubble", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      permissionMsg("2026-05-01T01:00:01Z", "perm"),
      result("2026-05-01T01:00:01Z", "perm", '{"request_id":"r1"}'),
      userMsg(
        "2026-05-01T01:00:02Z",
        "Your permission request for Slack was granted with the following permissions: slack-read-all. Please retry the call that was blocked.",
        "u-res",
      ),
    ];
    const sections = run(events);
    // The card (in the first turn) carries the verdict...
    expect(sections[0].items.map((i) => i.kind)).toEqual(["permission"]);
    expect((sections[0].items[0] as PermissionItem).resolution).toBe("granted");
    // ...and the notification opened a new turn rather than rendering as a
    // user prompt: a second section exists with no user bubble, and the raw
    // "was granted" text appears as no section's user message.
    expect(sections).toHaveLength(2);
    expect(sections[1].user_event).toBeNull();
    expect(sections.every((s) => !(s.user_event?.content ?? "").includes("was granted"))).toBe(true);
  });

  it("carries an open step over the approval so it continues in the new turn", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "show me my gmail unreads"),
      tkMsg("2026-05-01T01:00:01Z", "tk start s1", "c-s1"),
      result("2026-05-01T01:00:01Z", "c-s1", startOut("s1", "Connect to your Gmail account")),
      permissionMsg("2026-05-01T01:00:02Z", "perm"),
      result("2026-05-01T01:00:02Z", "perm", '{"request_id":"r1"}'),
      userMsg("2026-05-01T01:00:03Z", "Your permission request for Gmail was granted. Retry.", "u-res"),
      // Post-approval work and the step closing happen after the boundary.
      workMsg("2026-05-01T01:00:04Z", "Bash", "w-after"),
      result("2026-05-01T01:00:04Z", "w-after", "ok"),
      tkMsg("2026-05-01T01:00:05Z", "tk close s1", "x-s1"),
      result("2026-05-01T01:00:05Z", "x-s1", closeOut("s1", "Connect to your Gmail account")),
    ];
    const sections = run(events, /* idle */ false);
    expect(sections).toHaveLength(2);
    // First turn: the step (still open at the boundary) and the granted card.
    expect(sections[0].items.map((i) => i.kind)).toEqual(["step", "permission"]);
    const s1First = stepItems(sections[0].items)[0];
    expect(s1First.ticket_id).toBe("s1");
    expect(s1First.status).toBe("active");
    // Second turn: the step carried over, did the post-approval work, and closed.
    const s1Second = stepItems(sections[1].items)[0];
    expect(s1Second.ticket_id).toBe("s1");
    expect(s1Second.is_carryover).toBe(true);
    expect(s1Second.status).toBe("done");
    expect(s1Second.events.map((e) => e.event_id)).toEqual(["a-w-after"]);
    expect(sections[1].user_event).toBeNull();
  });

  it("marks the card denied", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      permissionMsg("2026-05-01T01:00:01Z", "perm"),
      result("2026-05-01T01:00:01Z", "perm", '{"request_id":"r1"}'),
      userMsg(
        "2026-05-01T01:00:02Z",
        "Your permission request for Slack was denied. Do not retry the blocked call.",
        "u-res",
      ),
    ];
    const sections = run(events);
    expect((sections[0].items[0] as PermissionItem).resolution).toBe("denied");
  });

  it("resolves the oldest open request first when several are outstanding", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      permissionMsg("2026-05-01T01:00:01Z", "perm1"),
      result("2026-05-01T01:00:01Z", "perm1", '{"request_id":"r1"}'),
      permissionMsg("2026-05-01T01:00:02Z", "perm2"),
      result("2026-05-01T01:00:02Z", "perm2", '{"request_id":"r2"}'),
      userMsg("2026-05-01T01:00:03Z", "Your permission request for Slack was granted. Retry.", "u-res1"),
      userMsg("2026-05-01T01:00:04Z", "Your permission request for GitHub was denied. Do not retry.", "u-res2"),
    ];
    const sections = run(events);
    const perms = sections[0].items.filter((i) => i.kind === "permission") as PermissionItem[];
    expect(perms).toHaveLength(2);
    expect(perms[0].resolution).toBe("granted"); // first request -> first verdict
    expect(perms[1].resolution).toBe("denied");
  });

  it("leaves a notification with no open request to render as a normal turn", () => {
    const events = [
      userMsg("2026-05-01T01:00:00Z", "go"),
      userMsg("2026-05-01T01:00:01Z", "Your permission request for Slack was granted. Retry.", "u-orphan"),
    ];
    const sections = run(events);
    // No card to claim it, so it falls through and opens its own turn.
    expect(sections).toHaveLength(2);
    expect(sections[1].user_event?.event_id).toBe("u-orphan");
  });
});
