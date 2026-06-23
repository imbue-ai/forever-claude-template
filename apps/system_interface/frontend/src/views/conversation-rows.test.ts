import { describe, expect, it } from "vitest";
import type { TranscriptEvent, ToolResultEvent, AssistantMessageEvent, UserMessageEvent } from "../models/Response";
import type { SectionView, StepNode } from "./turn-grouping";
import { buildConversationRows, buildRows, isSubagentRunning, sectionRendersUserBubble } from "./conversation-rows";

// --- Event builders (mirroring turn-grouping.test.ts) ---

function userMsg(ts: string, content: string, id = `u-${ts}`): UserMessageEvent {
  return { timestamp: ts, type: "user_message", event_id: id, source: "test", role: "user", content };
}

function assistantText(ts: string, text: string, stopReason: string | null = null): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${ts}`,
    source: "test",
    model: "m",
    text,
    tool_calls: [],
    stop_reason: stopReason,
    usage: null,
    is_auth_error: false,
  };
}

/** A tk lifecycle Bash call as it appears in the transcript. */
function tkMsg(ts: string, command: string, callId: string): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: "Bash", input_preview: JSON.stringify({ command }) }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

function result(callId: string, output: string): ToolResultEvent {
  return {
    timestamp: `${callId}-r`,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "Bash",
    output,
    is_error: false,
  };
}

describe("buildConversationRows", () => {
  // The point of the shared builder: a subagent's transcript runs the same
  // section -> rows pipeline as the main chat, so a turn that declares tk steps
  // renders as a single ProgressBlock (the timeline), not raw tk Bash calls.
  it("renders a turn with tk steps as one progress block", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "do the thing"),
      tkMsg("t2", "tk start cod-step-aaa", "c1"),
      result("c1", "Updated cod-step-aaa -> in_progress\ntk-step cod-step-aaa title: Look into it"),
      tkMsg("t3", 'tk close cod-step-aaa "looked into it"', "c2"),
      result(
        "c2",
        "Updated cod-step-aaa -> closed\ntk-step cod-step-aaa title: Look into it\ntk-step cod-step-aaa summary: looked into it",
      ),
    ];

    const rows = buildConversationRows("agent-1", events, /* agentIsIdle */ true);

    const userRow = rows.find((r) => r.key === "u-t1");
    expect(userRow).toBeDefined();
    const progressRows = rows.filter((r) => r.key.startsWith("progress-"));
    expect(progressRows).toHaveLength(1);
    // The raw tk Bash calls are folded into the progress block, not surfaced as
    // their own rows.
    expect(rows.some((r) => r.key === "a-c1" || r.key === "a-c2")).toBe(false);
  });

  it("renders a turn with no steps as plain user/assistant rows", () => {
    const events: TranscriptEvent[] = [userMsg("t1", "hello"), assistantText("t2", "hi there", "end_turn")];

    const rows = buildConversationRows("agent-1", events, true);

    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-t2"]);
    expect(rows.some((r) => r.key.startsWith("progress-"))).toBe(false);
  });
});

// --- Section fixtures for the turn-margin flush logic ---

function stepNode(id: string): StepNode {
  return {
    ticket_id: id,
    title: "A step",
    status: "active",
    summary: null,
    narration: null,
    is_carryover: false,
    is_frontier: true,
    events: [] as AssistantMessageEvent[],
  };
}

/** A section carrying a tk step (so buildRows emits a ProgressBlock row),
 *  opened by the given boundary user_event (null = a bubble-less boundary, the
 *  shape a permission grant/deny opens). */
function stepSection(key: string, user_event: UserMessageEvent | null): SectionView {
  return {
    user_event,
    key,
    items: [{ kind: "step", step: stepNode(`${key}-s1`) }],
    trailing_reply: [] as AssistantMessageEvent[],
  };
}

/** The isResumption attr buildRows passed to the ProgressBlock emitted for
 *  the section at `key`, read off the rendered vnode's attrs. */
function progressIsResumption(rows: ReturnType<typeof buildRows>, key: string): boolean | undefined {
  const row = rows.find((r) => r.key === `progress-${key}`);
  if (row === undefined) throw new Error(`no progress row for ${key}`);
  const vnode = row.render() as { attrs: { isResumption?: boolean } };
  return vnode.attrs.isResumption;
}

describe("sectionRendersUserBubble", () => {
  it("is true for a section opened by a real user message", () => {
    expect(sectionRendersUserBubble(stepSection("a", userMsg("t0", "show me my email")))).toBe(true);
  });

  it("is false for a bubble-less boundary section (user_event null)", () => {
    // The shape a permission grant/deny opens: the verdict folds onto the card
    // above and the section carries no user bubble.
    expect(sectionRendersUserBubble(stepSection("a", null))).toBe(false);
  });

  it("is false for a hidden user message (e.g. a skill expansion)", () => {
    expect(sectionRendersUserBubble(stepSection("a", userMsg("t0", "/welcome", "u-welcome")))).toBe(false);
  });
});

describe("buildRows resumption marking", () => {
  const noToolResults = new Map<string, ToolResultEvent>();

  it("marks a bubble-less non-first section's block as a resumption", () => {
    // Block B is the permission-resolution boundary: it is not the first section
    // and renders no user bubble, so its block carries the resumption marker.
    // Block A (the normal turn that issued the request) does not.
    const sections = [stepSection("a", userMsg("t0", "go")), stepSection("b", null)];
    const rows = buildRows("agent", sections, noToolResults);
    expect(progressIsResumption(rows, "b")).toBe(true);
    expect(progressIsResumption(rows, "a")).toBe(false);
  });

  it("does NOT mark a section that renders a normal user bubble", () => {
    const sections = [stepSection("a", userMsg("t0", "first")), stepSection("b", userMsg("t1", "second"))];
    const rows = buildRows("agent", sections, noToolResults);
    expect(progressIsResumption(rows, "b")).toBe(false);
  });

  it("does NOT mark the first section even when it renders no user bubble", () => {
    // The first `section-pre` is bubble-less but `s === 0`, so it is not a
    // resumption boundary -- only a later bubble-less section is.
    const sections = [stepSection("a", null)];
    const rows = buildRows("agent", sections, noToolResults);
    expect(progressIsResumption(rows, "a")).toBe(false);
  });
});

describe("isSubagentRunning", () => {
  it("is running while the last assistant turn has not terminally stopped", () => {
    expect(isSubagentRunning([assistantText("t1", "working", null)])).toBe(true);
    expect(isSubagentRunning([assistantText("t1", "calling a tool", "tool_use")])).toBe(true);
  });

  it("is settled once the last assistant turn stops", () => {
    expect(isSubagentRunning([assistantText("t1", "done", "end_turn")])).toBe(false);
  });

  it("is not running with no assistant turns", () => {
    expect(isSubagentRunning([userMsg("t1", "hi")])).toBe(false);
  });
});
