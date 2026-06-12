/**
 * Activity strip that sits just above the message input.
 *
 * The backend (system interface) is the source of truth for *which* state
 * the agent is in -- IDLE / THINKING / TOOL_RUNNING. The state is delivered
 * on each agent's ``activity_state`` field via the ``agents_updated`` WS
 * payload.
 *
 * The frontend's only job is to pick a label for the current state. For
 * TOOL_RUNNING we enrich the generic "Running tool…" label by walking the
 * transcript to find the most recent unmatched assistant tool call and
 * naming it specifically:
 *   - "Reading <basename>"        for Read
 *   - "Editing <basename>"        for Edit / MultiEdit
 *   - "Writing <basename>"        for Write
 *   - "Running <description>"     for Bash (the agent-supplied one-line
 *                                 description of the command; falls back
 *                                 to the raw command if absent)
 *   - "Searching <pattern>"       for Grep / Glob
 *   - "Loading skill <name>"      for Skill
 *   - "Searching the web <query>" for WebSearch
 *   - "Fetching page <url>"       for WebFetch
 *   - "Delegating to sub-agent…"  for Agent / Task
 *   - "Running <tool part>"       for MCP tools (parsed from the
 *                                 mcp__<namespace>__<tool> convention)
 *   - "Running <target>"          for any other unmapped tool that has a
 *                                 recognizable target in its input params
 *   - "Running tool…"             for fully unknown tools, or if the
 *                                 transcript hasn't surfaced the tool
 *                                 call yet (timing race).
 *
 * A null ``activity_state`` means the server has no per-agent activity
 * tracking for this agent (proto-agents, non-Claude agent types, remote
 * agents whose state dir is not on this host) -- the indicator collapses.
 */

import m from "mithril";
import type { ToolCall, TranscriptEvent } from "../models/Response";
import { getEffectiveActivityState } from "../models/PendingMessages";

// Note: Agent / Task are intentionally NOT in this map. labelForToolCall
// short-circuits with the "Delegating to sub-agent…" label for those tools
// before consulting this verb table.
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

const MAX_TARGET_LEN = 60;

function basename(p: string): string {
  const idx = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return idx >= 0 ? p.slice(idx + 1) : p;
}

function shorten(s: string, max: number): string {
  s = s.replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function targetForToolCall(tc: ToolCall): string | null {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(tc.input_preview) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;

  // Bash specifically: prefer the agent-supplied `description` (a short
  // human-readable phrase like "Check git status") over the raw command,
  // which is often noisy / truncated mid-flag.
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

/**
 * Parse an MCP tool name like "mcp__sculptor__ask_user_question" or
 * "mcp__plugin_playwright_playwright__browser_click" into a readable
 * label like "Running ask user question" or "Running browser click".
 *
 * Returns null for non-MCP names.
 */
function labelForMcpTool(name: string): string | null {
  if (!name.startsWith("mcp__")) return null;
  // Split on the double-underscore separator: "mcp__<namespace>__<tool>"
  const lastSep = name.lastIndexOf("__");
  if (lastSep <= 4) return null; // no tool part after the namespace
  const toolPart = name.slice(lastSep + 2);
  if (toolPart === "") return null;
  const readable = toolPart.replace(/_/g, " ");
  return `Running ${readable}`;
}

function labelForToolCall(tc: ToolCall): string {
  if (tc.tool_name === "Agent" || tc.tool_name === "Task") {
    return "Delegating to sub-agent…";
  }
  const verb = VERB_BY_TOOL[tc.tool_name];
  const target = targetForToolCall(tc);
  if (verb !== undefined && target !== null) return `${verb} ${target}`;
  if (verb !== undefined) return `${verb}…`;

  const mcpLabel = labelForMcpTool(tc.tool_name);
  if (mcpLabel !== null) return mcpLabel;

  // Last resort: try to surface a target from the input params so the
  // user sees *something* descriptive rather than a bare "Running tool…".
  if (target !== null) return `Running ${target}`;
  return "Running tool…";
}

/**
 * Find the most recent assistant tool call whose tool_call_id has no
 * matching tool_result event. Returns null if none.
 */
function pendingToolCall(events: TranscriptEvent[]): ToolCall | null {
  const resolved = new Set<string>();
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "tool_result" && e.tool_call_id) {
      resolved.add(e.tool_call_id);
      continue;
    }
    if (e.type === "assistant_message" && e.tool_calls && e.tool_calls.length > 0) {
      for (let j = e.tool_calls.length - 1; j >= 0; j--) {
        const tc = e.tool_calls[j];
        if (!resolved.has(tc.tool_call_id)) {
          return tc;
        }
      }
    }
  }
  return null;
}

// Activity states in which the agent has a turn in progress that the user
// can interrupt. IDLE and null mean there is nothing to interrupt.
const WORKING_ACTIVITY_STATES: ReadonlySet<string> = new Set(["THINKING", "TOOL_RUNNING"]);

/**
 * Whether the given server-derived activity state means the agent is in the
 * middle of an interruptible turn. Drives the visibility of the stop button
 * in the message input.
 */
export function isWorkingActivityState(state: string | null | undefined): boolean {
  return state !== null && state !== undefined && WORKING_ACTIVITY_STATES.has(state);
}

/**
 * Pick the user-facing label for a given server-derived activity state.
 *
 * For TOOL_RUNNING we consult the transcript to enrich the label. For
 * every other state the label is fixed (or null = hide).
 */
export function labelForActivityState(state: string | null | undefined, events: TranscriptEvent[]): string | null {
  if (state === null || state === undefined) return null;
  if (state === "IDLE") return null;
  if (state === "THINKING") return "Thinking…";
  if (state === "TOOL_RUNNING") {
    const pending = pendingToolCall(events);
    if (pending !== null) return labelForToolCall(pending);
    return "Running tool…";
  }
  // Unknown / future enum value -- leave the slot collapsed.
  return null;
}

interface ActivityIndicatorAttrs {
  agentId: string;
  events: TranscriptEvent[];
}

export function ActivityIndicator(): m.Component<ActivityIndicatorAttrs> {
  return {
    view(vnode) {
      const state = getEffectiveActivityState(vnode.attrs.agentId);
      const label = labelForActivityState(state, vnode.attrs.events);
      if (label === null) return null;
      return m("div.agent-activity-indicator", { "data-state": state, role: "status", "aria-live": "polite" }, [
        m("span.agent-activity-indicator__dot"),
        m("span.agent-activity-indicator__label", label),
      ]);
    },
  };
}
