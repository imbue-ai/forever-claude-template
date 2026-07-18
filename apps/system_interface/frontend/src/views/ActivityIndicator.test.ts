import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type m from "mithril";
import type { TranscriptEvent } from "../models/Response";

// Mithril captures requestAnimationFrame at import time; polyfill before imports
// so the m.redraw() the skip handler fires does not throw in the node test env.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// The component reads the effective activity state from PendingMessages and, on
// skip, interrupts the agent then re-sends the last question on a fast model.
// Stub both models so the render tests control the state and observe the
// interrupt + sends (in order) without a live backend.
let mockActivityState: string | null = "THINKING";
vi.mock("../models/PendingMessages", () => ({
  getEffectiveActivityState: () => mockActivityState,
}));
// Records interrupt/send calls in the order they happen so a test can assert the
// fast-retry sequence (interrupt -> /model -> /effort -> resend).
const skipCalls: string[] = [];
const interruptAgentMock = vi.fn((agentId: string) => {
  skipCalls.push(`interrupt:${agentId}`);
  return Promise.resolve();
});
const sendMessageMock = vi.fn((agentId: string, message: string) => {
  skipCalls.push(`send:${agentId}:${message}`);
  return Promise.resolve();
});
vi.mock("../models/Response", () => ({
  interruptAgent: (agentId: string) => interruptAgentMock(agentId),
  sendMessage: (agentId: string, message: string) => sendMessageMock(agentId, message),
}));

import {
  ActivityIndicator,
  SKIP_THRESHOLD_MS,
  formatElapsed,
  isWorkingActivityState,
  labelForActivityState,
  lastUserPromptText,
  shouldOfferSkip,
} from "./ActivityIndicator";

// The vnode param type of the component's view, so the render test can build a
// minimal vnode without referencing the non-exported attrs interface.
type IndicatorVnode = Parameters<NonNullable<ReturnType<typeof ActivityIndicator>["view"]>>[0];

function indicatorVnode(agentId: string, events: TranscriptEvent[] = []): IndicatorVnode {
  return { attrs: { agentId, events } } as unknown as IndicatorVnode;
}

/** Depth-first search for the first vnode carrying the given CSS class. */
function findByClass(node: unknown, cls: string): m.Vnode | undefined {
  if (node === null || typeof node !== "object") return undefined;
  const v = node as m.Vnode;
  const className = (v.attrs as { className?: string } | null | undefined)?.className;
  if (typeof className === "string" && className.split(" ").includes(cls)) return v;
  const children = (v as { children?: unknown }).children;
  if (Array.isArray(children)) {
    for (const child of children) {
      const found = findByClass(child, cls);
      if (found !== undefined) return found;
    }
  }
  return undefined;
}

function userMsg(ts: string, content = "hi"): TranscriptEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", role: "user", content };
}

function toolUse(ts: string, toolName: string, callId: string, input: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "test-model",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_preview: input }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

function toolResult(ts: string, callId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "test-tool",
    output: "result",
    is_error: false,
  };
}

describe("labelForActivityState — fixed-label states", () => {
  it("hides the indicator for null state (server has no activity tracking for this agent)", () => {
    expect(labelForActivityState(null, [])).toBe(null);
  });

  it("hides the indicator for undefined state (pre-WS-connect)", () => {
    expect(labelForActivityState(undefined, [])).toBe(null);
  });

  it("hides the indicator for IDLE", () => {
    expect(labelForActivityState("IDLE", [userMsg("2026-04-28T01:00:00Z")])).toBe(null);
  });

  it("hides the indicator for an unknown / future state value", () => {
    expect(labelForActivityState("SOMETHING_NEW", [])).toBe(null);
  });

  it("returns 'Thinking…' for THINKING", () => {
    expect(labelForActivityState("THINKING", [userMsg("2026-04-28T01:00:00Z")])).toBe("Thinking…");
  });
});

describe("labelForActivityState — TOOL_RUNNING transcript enrichment", () => {
  it("labels Read with the file basename", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"src/themes/midnight.ts"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Reading midnight.ts");
  });

  it("labels Edit / MultiEdit with file basename", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Edit", "tc1", '{"file_path":"server/routes/reports.ts"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Editing reports.ts");
  });

  it("labels Bash with the agent-supplied description, not the raw command", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse(
        "2026-04-28T01:00:01Z",
        "Bash",
        "tc1",
        '{"command":"git status -uno --porcelain","description":"Check working tree status"}',
      ),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running Check working tree status");
  });

  it("falls back to the raw command when Bash has no description", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Bash", "tc1", '{"command":"npm test"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running npm test");
  });

  it("falls back to the raw command when Bash description is empty/whitespace", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Bash", "tc1", '{"command":"npm test","description":"   "}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running npm test");
  });

  it("labels Grep with the pattern in quotes", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Grep", "tc1", '{"pattern":"registerTheme"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe('Searching "registerTheme"');
  });

  it("labels Skill with 'Loading skill…' when no target", () => {
    const events = [userMsg("2026-04-28T01:00:00Z"), toolUse("2026-04-28T01:00:01Z", "Skill", "tc1", "{}")];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Loading skill…");
  });

  it("labels Skill with the skill name from the input", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Skill", "tc1", '{"skill":"autofix"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Loading skill autofix");
  });

  it("labels WebSearch with the search query", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "WebSearch", "tc1", '{"query":"playwright MCP setup"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe('Searching the web "playwright MCP setup"');
  });

  it("labels WebFetch with the URL from the input", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "WebFetch", "tc1", '{"url":"https://example.com/docs","prompt":"summarize"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Fetching page https://example.com/docs");
  });

  it("labels WebFetch with 'Fetching page…' when no target", () => {
    const events = [userMsg("2026-04-28T01:00:00Z"), toolUse("2026-04-28T01:00:01Z", "WebFetch", "tc1", "{}")];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Fetching page…");
  });

  it("labels MCP tools by parsing the tool name", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "mcp__plugin_playwright_playwright__browser_click", "tc1", "{}"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running browser click");
  });

  it("labels MCP tools with simple namespace", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "mcp__sculptor__ask_user_question", "tc1", "{}"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running ask user question");
  });

  it("falls back to target from input params for unknown non-MCP tools", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "SomeNewTool", "tc1", '{"description":"doing something useful"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running doing something useful");
  });

  it("falls back to 'Running tool…' for unknown tools with no parseable input", () => {
    const events = [userMsg("2026-04-28T01:00:00Z"), toolUse("2026-04-28T01:00:01Z", "SomeNewTool", "tc1", "{}")];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running tool…");
  });

  it("returns 'Delegating to sub-agent…' for Agent / Task tools", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Agent", "tc1", '{"description":"do it"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Delegating to sub-agent…");
  });

  it("ignores tool calls that already have a matching tool_result when picking the pending one", () => {
    // First tool already resolved, second tool is the active one.
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"old.ts"}'),
      toolResult("2026-04-28T01:00:02Z", "tc1"),
      toolUse("2026-04-28T01:00:03Z", "Read", "tc2", '{"file_path":"new.ts"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Reading new.ts");
  });

  it("falls back to 'Running tool…' when state is TOOL_RUNNING but no pending tool call is visible yet (timing race)", () => {
    // Backend already detected a tool_use, but the frontend's transcript
    // stream hasn't surfaced it yet, or every visible tool_use has been
    // matched by a tool_result on the frontend's side.
    expect(labelForActivityState("TOOL_RUNNING", [userMsg("2026-04-28T01:00:00Z")])).toBe("Running tool…");
  });
});

describe("isWorkingActivityState — stop-button visibility gate", () => {
  it("treats THINKING / TOOL_RUNNING as an interruptible turn", () => {
    expect(isWorkingActivityState("THINKING")).toBe(true);
    expect(isWorkingActivityState("TOOL_RUNNING")).toBe(true);
  });

  it("treats IDLE as not working (nothing to interrupt)", () => {
    expect(isWorkingActivityState("IDLE")).toBe(false);
  });

  it("treats null / undefined (no activity tracking) as not working", () => {
    expect(isWorkingActivityState(null)).toBe(false);
    expect(isWorkingActivityState(undefined)).toBe(false);
  });

  it("treats an unknown / future state value as not working", () => {
    expect(isWorkingActivityState("SOMETHING_NEW")).toBe(false);
  });
});

describe("lastUserPromptText — the prompt a fast retry re-asks", () => {
  it("returns null when there are no user messages", () => {
    expect(lastUserPromptText([])).toBe(null);
    expect(lastUserPromptText([toolUse("2026-04-28T01:00:00Z", "Read", "tc1", "{}")])).toBe(null);
  });

  it("returns the most recent genuine user message content", () => {
    const events = [userMsg("2026-04-28T01:00:00Z", "first"), userMsg("2026-04-28T01:00:01Z", "second")];
    expect(lastUserPromptText(events)).toBe("second");
  });

  it("skips hidden control chatter (/model, /effort, local-command-stdout) and skill expansions", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z", "the real question"),
      userMsg("2026-04-28T01:00:01Z", "/model haiku"),
      userMsg("2026-04-28T01:00:02Z", "<local-command-stdout>Set model to Haiku 4.5</local-command-stdout>"),
      userMsg("2026-04-28T01:00:03Z", "/effort low"),
    ];
    expect(lastUserPromptText(events)).toBe("the real question");
  });

  it("skips empty/whitespace-only user messages", () => {
    const events = [userMsg("2026-04-28T01:00:00Z", "real"), userMsg("2026-04-28T01:00:01Z", "   ")];
    expect(lastUserPromptText(events)).toBe("real");
  });
});

describe("shouldOfferSkip — time-gated skip control", () => {
  it("hides the skip control before the threshold", () => {
    expect(shouldOfferSkip(0)).toBe(false);
    expect(shouldOfferSkip(SKIP_THRESHOLD_MS - 1)).toBe(false);
  });

  it("shows the skip control at and after the threshold", () => {
    expect(shouldOfferSkip(SKIP_THRESHOLD_MS)).toBe(true);
    expect(shouldOfferSkip(SKIP_THRESHOLD_MS + 60_000)).toBe(true);
  });
});

describe("formatElapsed — m:ss elapsed formatting", () => {
  it("formats sub-minute durations with a zero minute and padded seconds", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(5_000)).toBe("0:05");
    expect(formatElapsed(34_000)).toBe("0:34");
  });

  it("rolls seconds into minutes past 60s", () => {
    expect(formatElapsed(60_000)).toBe("1:00");
    expect(formatElapsed(95_000)).toBe("1:35");
    expect(formatElapsed(600_000)).toBe("10:00");
  });

  it("floors partial seconds rather than rounding", () => {
    expect(formatElapsed(1_999)).toBe("0:01");
  });

  it("clamps negative durations (clock skew) to zero", () => {
    expect(formatElapsed(-500)).toBe("0:00");
  });
});

describe("ActivityIndicator component — time-gated skip control", () => {
  let nowSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    mockActivityState = "THINKING";
    interruptAgentMock.mockClear();
    sendMessageMock.mockClear();
    skipCalls.length = 0;
    nowSpy = vi.spyOn(Date, "now");
  });

  afterEach(() => {
    nowSpy.mockRestore();
  });

  it("hides the skip control before the threshold, then shows it (with a timer) once the turn runs long", () => {
    const comp = ActivityIndicator();
    const vnode = indicatorVnode("agent-slow");

    // First render establishes the working-start clock; nothing has elapsed yet.
    nowSpy.mockReturnValue(1_000);
    const early = comp.view!(vnode);
    expect(findByClass(early, "agent-activity-indicator__skip")).toBeUndefined();
    expect(findByClass(early, "agent-activity-indicator__elapsed")).toBeUndefined();

    // Advance past the threshold: the skip control and elapsed timer appear.
    nowSpy.mockReturnValue(1_000 + SKIP_THRESHOLD_MS + 5_000);
    const late = comp.view!(vnode);
    expect(findByClass(late, "agent-activity-indicator__skip")).toBeDefined();
    expect(findByClass(late, "agent-activity-indicator__elapsed")).toBeDefined();
  });

  it("interrupts the agent when the skip control is clicked (no prior prompt to re-ask)", () => {
    const comp = ActivityIndicator();
    // No user_message in events -> nothing to re-ask, so a plain interrupt.
    const vnode = indicatorVnode("agent-clickme");

    nowSpy.mockReturnValue(0);
    comp.view!(vnode);
    nowSpy.mockReturnValue(SKIP_THRESHOLD_MS + 1_000);
    const tree = comp.view!(vnode);

    const skip = findByClass(tree, "agent-activity-indicator__skip");
    expect(skip).toBeDefined();
    (skip!.attrs as { onclick: () => void }).onclick();
    expect(interruptAgentMock).toHaveBeenCalledWith("agent-clickme");
    // Nothing to re-ask, so no model switch or resend.
    expect(sendMessageMock).not.toHaveBeenCalled();
  });

  it("fast-retries when clicked: interrupt, switch to the fast model, then re-ask the last question", async () => {
    const comp = ActivityIndicator();
    const events = [userMsg("2026-04-28T01:00:00Z", "What is the meaning of life?")];
    const vnode = indicatorVnode("agent-retry", events);

    nowSpy.mockReturnValue(0);
    comp.view!(vnode);
    nowSpy.mockReturnValue(SKIP_THRESHOLD_MS + 1_000);
    const tree = comp.view!(vnode);

    const skip = findByClass(tree, "agent-activity-indicator__skip");
    expect(skip).toBeDefined();
    (skip!.attrs as { onclick: () => Promise<void> }).onclick();
    // Let the awaited interrupt + sends resolve.
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Interrupt first, then /model haiku, /effort low, then the re-asked prompt,
    // strictly in that order.
    expect(skipCalls).toEqual([
      "interrupt:agent-retry",
      "send:agent-retry:/model haiku",
      "send:agent-retry:/effort low",
      "send:agent-retry:What is the meaning of life?",
    ]);
  });

  it("re-asks the genuine prompt, skipping a prior fast retry's own control chatter", async () => {
    const comp = ActivityIndicator();
    // A genuine prompt followed by the hidden control commands a prior fast retry
    // would have left in the transcript. The genuine prompt must be re-asked.
    const events = [
      userMsg("2026-04-28T01:00:00Z", "Summarize the report."),
      userMsg("2026-04-28T01:00:01Z", "/model haiku"),
      userMsg("2026-04-28T01:00:02Z", "<local-command-stdout>Set model to Haiku 4.5</local-command-stdout>"),
      userMsg("2026-04-28T01:00:03Z", "/effort low"),
    ];
    const vnode = indicatorVnode("agent-again", events);

    nowSpy.mockReturnValue(0);
    comp.view!(vnode);
    nowSpy.mockReturnValue(SKIP_THRESHOLD_MS + 1_000);
    const tree = comp.view!(vnode);

    const skip = findByClass(tree, "agent-activity-indicator__skip");
    (skip!.attrs as { onclick: () => Promise<void> }).onclick();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(skipCalls[skipCalls.length - 1]).toBe("send:agent-again:Summarize the report.");
  });

  it("renders nothing (and offers no skip) when the agent is idle", () => {
    mockActivityState = "IDLE";
    const comp = ActivityIndicator();
    nowSpy.mockReturnValue(SKIP_THRESHOLD_MS * 10);
    expect(comp.view!(indicatorVnode("agent-idle"))).toBe(null);
  });
});
