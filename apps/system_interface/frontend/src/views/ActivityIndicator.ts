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
import { interruptAgent, sendMessage } from "../models/Response";
import { getEffectiveActivityState } from "../models/PendingMessages";
import { isHiddenUserMessage } from "./message-classification";
import { describeRequestError } from "../models/request-error";
import { skipIcon } from "./icons";

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

// How long a turn must run continuously before the skip affordance appears.
// Below this, the indicator behaves exactly as before -- the skip control is a
// "this is taking too long" escape hatch, not an always-on button (the composer
// already has an always-visible stop button for that).
export const SKIP_THRESHOLD_MS = 20_000;

// How often the indicator re-renders while a turn is in flight, so the elapsed
// timer advances and the skip control appears without waiting for the next
// server push.
const TICK_INTERVAL_MS = 1_000;

/**
 * Whether a turn that has been working for ``elapsedMs`` has run long enough to
 * offer the skip control.
 */
export function shouldOfferSkip(elapsedMs: number): boolean {
  return elapsedMs >= SKIP_THRESHOLD_MS;
}

/**
 * Format an elapsed duration as ``m:ss`` (e.g. 34s -> "0:34", 95s -> "1:35").
 * Clamps negatives to zero so a clock skew never renders "-0:01".
 */
export function formatElapsed(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

// The slash commands a fast retry sends -- after cancelling the slow turn -- to
// switch the agent to a quick, shallow reply before re-asking the question:
// Haiku with minimal thinking. Sent as chat messages because that is the only
// non-interactive way to switch (a bare "/model haiku" / "/effort low" line
// applies without a picker). They are best-effort (no delivery confirmation) and
// hidden from the chat by message-classification, so the user sees only the
// re-asked question and its fast answer.
const FAST_RETRY_MODEL_COMMAND = "/model haiku";
const FAST_RETRY_EFFORT_COMMAND = "/effort low";

/**
 * The text of the most recent genuine user prompt in ``events`` -- the message a
 * fast retry re-asks. Walks from the tail and skips hidden/control user_message
 * events (slash-command invocations, their stdout echoes, skill expansions) so a
 * prior fast retry's own ``/model`` / ``/effort`` chatter is never mistaken for
 * the prompt. Returns null when no genuine user message is present.
 */
export function lastUserPromptText(events: TranscriptEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type !== "user_message") continue;
    const content = e.content ?? "";
    if (content.trim() === "") continue;
    if (isHiddenUserMessage(content)) continue;
    return content;
  }
  return null;
}

// Per-agent timestamp (epoch ms) of when the current working spell began. Keyed
// by agentId and module-scoped so it survives component remounts (e.g. the panel
// re-rendering) and stays consistent if the same agent is shown in two panels.
// Cleared the moment the agent leaves a working state, so each new turn restarts
// the clock.
const workingSinceByAgent = new Map<string, number>();

interface ActivityIndicatorAttrs {
  agentId: string;
  events: TranscriptEvent[];
}

export function ActivityIndicator(): m.Component<ActivityIndicatorAttrs> {
  let tickHandle: ReturnType<typeof setInterval> | null = null;
  // True while the current turn is in a working state; gates the ticker so we
  // only force redraws (for the advancing timer) when there is a turn to time.
  let isWorking = false;
  let isSkipInFlight = false;

  // Fast retry: cancel the slow in-flight turn and immediately re-ask the same
  // question, answered by a quick, shallow model (Haiku, minimal thinking) so the
  // user gets a fast answer instead of waiting out the slow deep one.
  //
  // Sequence: interrupt (the only cancel primitive -- it restarts the agent to an
  // idle state, preserving history but resetting the model to the opus default),
  // then switch to the fast model and re-ask. The switch is not restored
  // afterwards: the restore commands would have to be sent to a busy agent, where
  // delivery is unreliable, so the agent stays on the fast model until the next
  // interrupt (which resets it) or a manual /model change. Re-asking is skipped
  // only when there is no genuine prior user message to re-ask, in which case the
  // control still cancels the turn (a plain interrupt).
  async function handleSkip(agentId: string, events: TranscriptEvent[]): Promise<void> {
    if (isSkipInFlight) return;
    const prompt = lastUserPromptText(events);
    isSkipInFlight = true;
    m.redraw();
    try {
      await interruptAgent(agentId);
      // The turn is over; drop the clock so a subsequent turn starts fresh.
      workingSinceByAgent.delete(agentId);
      if (prompt !== null) {
        // Best-effort model switch, then re-ask the same question. Ordering is
        // sequential so the switch lands before the resend starts a turn.
        await sendMessage(agentId, FAST_RETRY_MODEL_COMMAND);
        await sendMessage(agentId, FAST_RETRY_EFFORT_COMMAND);
        await sendMessage(agentId, prompt);
      }
    } catch (err) {
      const detail = describeRequestError(err);
      console.error(`Failed to fast-retry agent ${agentId}: ${detail}`);
      // The user deliberately asked to retry; surface a failure the same way the
      // composer surfaces send failures (matches the alert convention in
      // MessageInput).
      alert(`Failed to answer faster: ${detail}`);
    } finally {
      isSkipInFlight = false;
      m.redraw();
    }
  }

  return {
    oncreate() {
      tickHandle = setInterval(() => {
        // Only redraw while a turn is running -- an idle agent has no timer to
        // advance, so this stays quiet the rest of the time.
        if (isWorking) m.redraw();
      }, TICK_INTERVAL_MS);
    },
    onremove() {
      if (tickHandle !== null) {
        clearInterval(tickHandle);
        tickHandle = null;
      }
    },
    view(vnode) {
      const agentId = vnode.attrs.agentId;
      const state = getEffectiveActivityState(agentId);
      const label = labelForActivityState(state, vnode.attrs.events);

      isWorking = isWorkingActivityState(state);
      if (!isWorking) {
        // Not in a working turn: clear the clock so the next turn starts at 0,
        // and reset the in-flight guard (the skip resolved into an idle state).
        workingSinceByAgent.delete(agentId);
        isSkipInFlight = false;
      }

      if (label === null) return null;

      let elapsedMs = 0;
      let offerSkip = false;
      if (isWorking) {
        const now = Date.now();
        const startedAt = workingSinceByAgent.get(agentId);
        if (startedAt === undefined) {
          workingSinceByAgent.set(agentId, now);
        } else {
          elapsedMs = now - startedAt;
        }
        offerSkip = shouldOfferSkip(elapsedMs);
      }

      return m("div.agent-activity-indicator", { "data-state": state, role: "status", "aria-live": "polite" }, [
        m("span.agent-activity-indicator__dot"),
        m("span.agent-activity-indicator__label", label),
        offerSkip
          ? m(
              "span.agent-activity-indicator__elapsed",
              { title: "Taking longer than usual" },
              formatElapsed(elapsedMs),
            )
          : null,
        offerSkip
          ? m(
              "button.agent-activity-indicator__skip",
              {
                type: "button",
                title: "Skip the wait: cancel this and answer the same question faster",
                disabled: isSkipInFlight,
                onclick: () => handleSkip(agentId, vnode.attrs.events),
              },
              [m.trust(skipIcon(12)), m("span", "Skip")],
            )
          : null,
      ]);
    },
  };
}
