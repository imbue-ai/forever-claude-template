import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import { isWorkingActivityState, labelForActivityState } from "./ActivityIndicator";

function userMsg(ts: string): TranscriptEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", role: "user", content: "hi" };
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
    expect(labelForActivityState(null, [], "claude")).toBe(null);
  });

  it("hides the indicator for undefined state (pre-WS-connect)", () => {
    expect(labelForActivityState(undefined, [], "claude")).toBe(null);
  });

  it("hides the indicator for IDLE", () => {
    expect(labelForActivityState("IDLE", [userMsg("2026-04-28T01:00:00Z")], "codex")).toBe(null);
  });

  it("hides the indicator for an unknown / future state value", () => {
    expect(labelForActivityState("SOMETHING_NEW", [], "claude")).toBe(null);
  });

  it("returns 'Thinking…' for THINKING (harness-independent)", () => {
    expect(labelForActivityState("THINKING", [userMsg("2026-04-28T01:00:00Z")], "claude")).toBe("Thinking…");
    expect(labelForActivityState("THINKING", [userMsg("2026-04-28T01:00:00Z")], "codex")).toBe("Thinking…");
  });
});

describe("labelForActivityState — TOOL_RUNNING harness routing", () => {
  it("routes to the claude caption for a claude agent", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"src/midnight.ts"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events, "claude")).toBe("Reading midnight.ts");
  });

  it("routes to the codex caption for a codex agent (code-mode exec)", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "exec", "tc1", 'await tools.exec_command({"cmd":"ls -la"})'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events, "codex")).toBe("Running ls -la");
  });

  it("picks the most recent unmatched tool call, skipping resolved ones", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"old.ts"}'),
      toolResult("2026-04-28T01:00:02Z", "tc1"),
      toolUse("2026-04-28T01:00:03Z", "Read", "tc2", '{"file_path":"new.ts"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events, "claude")).toBe("Reading new.ts");
  });

  it("falls back to 'Running tool…' when no pending tool call is visible yet (timing race)", () => {
    expect(labelForActivityState("TOOL_RUNNING", [userMsg("2026-04-28T01:00:00Z")], "codex")).toBe("Running tool…");
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
