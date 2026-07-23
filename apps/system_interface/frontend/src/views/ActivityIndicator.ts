/**
 * Activity strip that sits just above the message input -- the harness-common shell.
 *
 * The backend (system interface) is the source of truth for *which* state the agent
 * is in -- IDLE / THINKING / TOOL_RUNNING -- delivered on ``activity_state`` via the
 * ``agents_updated`` WS payload. This component's job is to render a label:
 *   - IDLE / null      -> hidden
 *   - THINKING         -> "Thinking…"
 *   - TOOL_RUNNING     -> the in-flight tool call, captioned by the agent's harness
 *
 * The TOOL_RUNNING caption is the only harness-specific bit; it is routed by the
 * agent's ``harness`` to a peer module (``claudeCaption`` / ``codexCaption``) -- neither
 * is a fallthrough default. A null ``activity_state`` means the server has no per-agent
 * activity tracking for this agent (proto-agents, remote agents) -- the strip collapses.
 */

import m from "mithril";
import type { ToolCall, TranscriptEvent } from "../models/Response";
import { getEffectiveActivityState } from "../models/PendingMessages";
import { claudeToolLabel } from "./claudeCaption";
import { codexToolLabel } from "./codexCaption";

/**
 * Find the most recent assistant tool call whose tool_call_id has no matching
 * tool_result event. Returns null if none. (Harness-agnostic: both parsers emit the
 * same ``assistant_message`` / ``tool_result`` shape.)
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

// Activity states in which the agent has an interruptible turn in progress.
const WORKING_ACTIVITY_STATES: ReadonlySet<string> = new Set(["THINKING", "TOOL_RUNNING"]);

/**
 * Whether the given server-derived activity state means the agent is in the
 * middle of an interruptible turn. Drives the visibility of the stop button.
 */
export function isWorkingActivityState(state: string | null | undefined): boolean {
  return state !== null && state !== undefined && WORKING_ACTIVITY_STATES.has(state);
}

/** The in-flight tool caption for the agent's harness. */
function labelForToolCall(tc: ToolCall, harness: string): string {
  return harness === "codex" ? codexToolLabel(tc) : claudeToolLabel(tc);
}

/**
 * Pick the user-facing label for a server-derived activity state. For TOOL_RUNNING
 * we consult the transcript for the in-flight tool and caption it per harness; every
 * other state is fixed (or null = hide).
 */
export function labelForActivityState(
  state: string | null | undefined,
  events: TranscriptEvent[],
  harness: string,
): string | null {
  if (state === null || state === undefined) return null;
  if (state === "IDLE") return null;
  if (state === "THINKING") return "Thinking…";
  if (state === "TOOL_RUNNING") {
    const pending = pendingToolCall(events);
    if (pending !== null) return labelForToolCall(pending, harness);
    return "Running tool…";
  }
  return null;
}

interface ActivityIndicatorAttrs {
  agentId: string;
  events: TranscriptEvent[];
  harness: string;
}

export function ActivityIndicator(): m.Component<ActivityIndicatorAttrs> {
  return {
    view(vnode) {
      const state = getEffectiveActivityState(vnode.attrs.agentId);
      const label = labelForActivityState(state, vnode.attrs.events, vnode.attrs.harness);
      if (label === null) return null;
      return m("div.agent-activity-indicator", { "data-state": state, role: "status", "aria-live": "polite" }, [
        m("span.agent-activity-indicator__dot"),
        m("span.agent-activity-indicator__label", label),
      ]);
    },
  };
}
