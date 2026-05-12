import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import { labelForActivityState } from "./ActivityIndicator";

function userMsg(ts: string): TranscriptEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", content: "hi" };
}

function toolUse(ts: string, toolName: string, callId: string, input: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_preview: input }],
  };
}

function toolResult(ts: string, callId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    output: "result",
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

  it("returns 'Waiting for permission' for WAITING_ON_PERMISSION", () => {
    expect(labelForActivityState("WAITING_ON_PERMISSION", [])).toBe("Waiting for permission");
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

  it("falls back to 'Running tool…' for unknown tools", () => {
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
