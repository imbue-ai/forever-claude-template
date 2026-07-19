/**
 * Activity strip that sits just above the message input.
 *
 * The backend (system interface) is the single source of truth for BOTH the
 * state (IDLE / THINKING / TOOL_RUNNING) and, for TOOL_RUNNING, the caption
 * ("Editing foo.py", "Running code", "Searching the web …"). Both ride the
 * ``agents_updated`` WS payload (``activity_state`` / ``activity_caption``).
 *
 * This component is a pure renderer: it reads the server-provided state and
 * caption and picks a label. All harness-specific caption logic now lives on
 * the backend (``activity_caption.py``) so the browser stays harness-blind.
 */

import m from "mithril";
import { getActivityCaption, getEffectiveActivityState } from "../models/PendingMessages";

// Activity states in which the agent has a turn in progress that the user can
// interrupt. IDLE and null mean there is nothing to interrupt.
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
 * Pick the user-facing label for a state + its server-provided caption.
 *
 * THINKING / IDLE are fixed (or hidden); TOOL_RUNNING shows the backend caption,
 * falling back to a generic label if the server sent none (e.g. the transcript
 * hasn't surfaced the tool call yet).
 */
export function labelForActivityState(
  state: string | null | undefined,
  caption: string | null | undefined,
): string | null {
  if (state === null || state === undefined) return null;
  if (state === "IDLE") return null;
  if (state === "THINKING") return "Thinking…";
  if (state === "TOOL_RUNNING") return caption ?? "Running tool…";
  // Unknown / future enum value -- leave the slot collapsed.
  return null;
}

interface ActivityIndicatorAttrs {
  agentId: string;
}

export function ActivityIndicator(): m.Component<ActivityIndicatorAttrs> {
  return {
    view(vnode) {
      const state = getEffectiveActivityState(vnode.attrs.agentId);
      const caption = getActivityCaption(vnode.attrs.agentId);
      const label = labelForActivityState(state, caption);
      if (label === null) return null;
      return m("div.agent-activity-indicator", { "data-state": state, role: "status", "aria-live": "polite" }, [
        m("span.agent-activity-indicator__dot"),
        m("span.agent-activity-indicator__label", label),
      ]);
    },
  };
}
