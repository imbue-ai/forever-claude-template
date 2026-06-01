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
