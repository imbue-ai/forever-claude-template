import { describe, expect, it } from "vitest";
import type { ToolCall } from "../models/Response";
import { claudeToolLabel } from "./claudeCaption";

function tc(tool_name: string, input_preview: string): ToolCall {
  return { tool_call_id: "tc1", tool_name, input_preview };
}

describe("claudeToolLabel", () => {
  it("labels Read with the file basename", () => {
    expect(claudeToolLabel(tc("Read", '{"file_path":"src/themes/midnight.ts"}'))).toBe("Reading midnight.ts");
  });

  it("labels Edit / MultiEdit with file basename", () => {
    expect(claudeToolLabel(tc("Edit", '{"file_path":"server/routes/reports.ts"}'))).toBe("Editing reports.ts");
  });

  it("labels Bash with the agent-supplied description, not the raw command", () => {
    expect(
      claudeToolLabel(
        tc("Bash", '{"command":"git status -uno --porcelain","description":"Check working tree status"}'),
      ),
    ).toBe("Running Check working tree status");
  });

  it("falls back to the raw command when Bash has no description", () => {
    expect(claudeToolLabel(tc("Bash", '{"command":"npm test"}'))).toBe("Running npm test");
  });

  it("falls back to the raw command when Bash description is empty/whitespace", () => {
    expect(claudeToolLabel(tc("Bash", '{"command":"npm test","description":"   "}'))).toBe("Running npm test");
  });

  it("labels Grep with the pattern in quotes", () => {
    expect(claudeToolLabel(tc("Grep", '{"pattern":"registerTheme"}'))).toBe('Searching "registerTheme"');
  });

  it("labels Skill with 'Loading skill…' when no target", () => {
    expect(claudeToolLabel(tc("Skill", "{}"))).toBe("Loading skill…");
  });

  it("labels Skill with the skill name from the input", () => {
    expect(claudeToolLabel(tc("Skill", '{"skill":"autofix"}'))).toBe("Loading skill autofix");
  });

  it("labels WebSearch with the search query", () => {
    expect(claudeToolLabel(tc("WebSearch", '{"query":"playwright MCP setup"}'))).toBe(
      'Searching the web "playwright MCP setup"',
    );
  });

  it("labels WebFetch with the URL from the input", () => {
    expect(claudeToolLabel(tc("WebFetch", '{"url":"https://example.com/docs","prompt":"summarize"}'))).toBe(
      "Fetching page https://example.com/docs",
    );
  });

  it("labels WebFetch with 'Fetching page…' when no target", () => {
    expect(claudeToolLabel(tc("WebFetch", "{}"))).toBe("Fetching page…");
  });

  it("labels MCP tools by parsing the tool name", () => {
    expect(claudeToolLabel(tc("mcp__plugin_playwright_playwright__browser_click", "{}"))).toBe(
      "Running browser click",
    );
  });

  it("labels MCP tools with simple namespace", () => {
    expect(claudeToolLabel(tc("mcp__sculptor__ask_user_question", "{}"))).toBe("Running ask user question");
  });

  it("falls back to target from input params for unknown non-MCP tools", () => {
    expect(claudeToolLabel(tc("SomeNewTool", '{"description":"doing something useful"}'))).toBe(
      "Running doing something useful",
    );
  });

  it("falls back to 'Running tool…' for unknown tools with no parseable input", () => {
    expect(claudeToolLabel(tc("SomeNewTool", "{}"))).toBe("Running tool…");
  });

  it("returns 'Delegating to sub-agent…' for Agent / Task tools", () => {
    expect(claudeToolLabel(tc("Agent", '{"description":"do it"}'))).toBe("Delegating to sub-agent…");
    expect(claudeToolLabel(tc("Task", "{}"))).toBe("Delegating to sub-agent…");
  });
});
