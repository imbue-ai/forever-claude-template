/**
 * Claude's TOOL_RUNNING caption: map an in-flight tool call to a verb + target.
 * The claude peer of ``codexCaption``; ``ActivityIndicator`` routes to one or the
 * other by the agent's harness.
 *
 *   - "Reading <basename>"        for Read
 *   - "Editing <basename>"        for Edit / MultiEdit
 *   - "Running <description>"     for Bash (agent-supplied description, else command)
 *   - "Searching the web <query>" for WebSearch
 *   - "Delegating to sub-agent…"  for Agent / Task
 *   - "Running <tool part>"       for mcp__<namespace>__<tool>
 *   - "Running tool…"             for anything unrecognised
 */

import type { ToolCall } from "../models/Response";
import { MAX_TARGET_LEN, basename, shorten } from "./captionUtils";

// Note: Agent / Task are handled separately (Delegating…) before this table.
const VERB_BY_TOOL: Record<string, string> = {
  Read: "Reading",
  Edit: "Editing",
  MultiEdit: "Editing",
  Write: "Writing",
  Bash: "Running",
  Grep: "Searching",
  Glob: "Searching",
  Skill: "Loading skill",
  ToolSearch: "Loading tool",
  WebSearch: "Searching the web",
  WebFetch: "Fetching page",
  LSP: "Querying language server",
  NotebookEdit: "Editing notebook",
  Monitor: "Monitoring",
  SendMessage: "Sending message",
};

function targetForToolCall(tc: ToolCall): string | null {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(tc.input_preview) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;

  // Bash: prefer the agent-supplied `description` over the raw (often truncated) command.
  if (tc.tool_name === "Bash") {
    const description = typeof parsed.description === "string" ? parsed.description : null;
    if (description !== null && description.trim() !== "") return shorten(description, MAX_TARGET_LEN);
    const command = typeof parsed.command === "string" ? parsed.command : null;
    if (command !== null) return shorten(command, MAX_TARGET_LEN);
    return null;
  }

  const filePath = typeof parsed.file_path === "string" ? parsed.file_path : null;
  if (filePath !== null) return basename(filePath);
  const path = typeof parsed.path === "string" ? parsed.path : null;
  if (path !== null) return basename(path);
  const url = typeof parsed.url === "string" ? parsed.url : null;
  if (url !== null) return shorten(url, MAX_TARGET_LEN);
  const command = typeof parsed.command === "string" ? parsed.command : null;
  if (command !== null) return shorten(command, MAX_TARGET_LEN);
  const pattern = typeof parsed.pattern === "string" ? parsed.pattern : null;
  if (pattern !== null) return `"${shorten(pattern, MAX_TARGET_LEN)}"`;
  const query = typeof parsed.query === "string" ? parsed.query : null;
  if (query !== null) return `"${shorten(query, MAX_TARGET_LEN)}"`;
  const skill = typeof parsed.skill === "string" ? parsed.skill : null;
  if (skill !== null) return shorten(skill, MAX_TARGET_LEN);
  const description = typeof parsed.description === "string" ? parsed.description : null;
  if (description !== null) return shorten(description, MAX_TARGET_LEN);
  return null;
}

/** "mcp__<namespace>__<tool>" -> "Running <tool with spaces>"; null for non-MCP names. */
function labelForMcpTool(name: string): string | null {
  if (!name.startsWith("mcp__")) return null;
  const lastSep = name.lastIndexOf("__");
  if (lastSep <= 4) return null;
  const toolPart = name.slice(lastSep + 2);
  if (toolPart === "") return null;
  return `Running ${toolPart.replace(/_/g, " ")}`;
}

export function claudeToolLabel(tc: ToolCall): string {
  if (tc.tool_name === "Agent" || tc.tool_name === "Task") {
    return "Delegating to sub-agent…";
  }
  const verb = VERB_BY_TOOL[tc.tool_name];
  const target = targetForToolCall(tc);
  if (verb !== undefined && target !== null) return `${verb} ${target}`;
  if (verb !== undefined) return `${verb}…`;

  const mcpLabel = labelForMcpTool(tc.tool_name);
  if (mcpLabel !== null) return mcpLabel;

  if (target !== null) return `Running ${target}`;
  return "Running tool…";
}
