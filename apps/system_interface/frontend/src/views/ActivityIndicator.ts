/**
 * Activity strip that sits just above the message input.
 *
 * The backend (system interface) is the source of truth for *which* state
 * the agent is in -- IDLE / THINKING / TOOL_RUNNING / WAITING_ON_PERMISSION
 * -- because the WAITING_ON_PERMISSION case relies on a marker file that
 * the transcript alone cannot detect. The state is delivered on each
 * agent's ``activity_state`` field via the ``agents_updated`` WS payload.
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
 *   - "Delegating to sub-agent…"  for Agent / Task
 *   - "Running tool…"             for any other unmapped tool, or if the
 *                                 transcript hasn't surfaced the tool
 *                                 call yet (timing race).
 *
 * A null ``activity_state`` means the server has no per-agent activity
 * tracking for this agent (proto-agents, non-Claude agent types, remote
 * agents whose state dir is not on this host) -- the indicator collapses.
 */

import m from "mithril";
import type { ToolCall, TranscriptEvent } from "../models/Response";
import { getAgentById } from "../models/AgentManager";

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
  const command = typeof parsed.command === "string" ? parsed.command : null;
  if (command !== null) return shorten(command, MAX_TARGET_LEN);
  const pattern = typeof parsed.pattern === "string" ? parsed.pattern : null;
  if (pattern !== null) return `"${shorten(pattern, MAX_TARGET_LEN)}"`;
  const description = typeof parsed.description === "string" ? parsed.description : null;
  if (description !== null) return shorten(description, MAX_TARGET_LEN);
  return null;
}

function labelForToolCall(tc: ToolCall): string {
  if (tc.tool_name === "Agent" || tc.tool_name === "Task") {
    return "Delegating to sub-agent…";
  }
  const verb = VERB_BY_TOOL[tc.tool_name];
  const target = targetForToolCall(tc);
  if (verb !== undefined && target !== null) return `${verb} ${target}`;
  if (verb !== undefined) return `${verb}…`;
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

/**
 * Pick the user-facing label for a given server-derived activity state.
 *
 * For TOOL_RUNNING we consult the transcript to enrich the label. For
 * every other state the label is fixed (or null = hide).
 */
export function labelForActivityState(state: string | null | undefined, events: TranscriptEvent[]): string | null {
  if (state === null || state === undefined) return null;
  if (state === "IDLE") return null;
  if (state === "WAITING_ON_PERMISSION") return "Waiting for permission";
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
      const agent = getAgentById(vnode.attrs.agentId);
      const state = agent?.activity_state ?? null;
      const label = labelForActivityState(state, vnode.attrs.events);
      if (label === null) return null;
      return m("div.agent-activity-indicator", { "data-state": state, role: "status", "aria-live": "polite" }, [
        m("span.agent-activity-indicator__dot"),
        m("span.agent-activity-indicator__label", label),
      ]);
    },
  };
}
