/**
 * Pure predicates that classify transcript events -- user_message content and
 * tool calls -- shared by the turn-grouping layer (deciding turn boundaries and
 * which events break the timeline) and the rendering layer (deciding inline
 * chrome and per-call affordances). They live in their own module, rather than
 * inside either layer, because both need to ask the same questions and neither
 * should depend on the other.
 *
 * The transcript stream uses the user_message type for several things other
 * than a genuine human turn: skill expansions, stop-hook feedback,
 * /welcome-style command invocations, etc. It also carries tool calls whose
 * shape determines how they render -- e.g. an agent permission request.
 */

import type { ToolCall } from "../models/Response";

/**
 * True for user_message events that are NOT a genuine user prompt --
 * skill expansions, stop-hook feedback, and command-name invocations
 * that Claude Code emits as user_message events while a single logical
 * turn is still in flight. These must not be treated as turn boundaries
 * (doing so splits one logical turn into several visible turns and
 * scatters the tasks across them).
 */
export function isNonBoundaryUserMessage(content: string): boolean {
  if (isHiddenUserMessage(content)) {
    return true;
  }
  if (isCollapsibleUserMessage(content) !== null) {
    return true;
  }
  return false;
}

/** True for the stop-hook feedback user_message Claude Code injects when a
 *  Stop hook fires. Distinct from skill expansions: a stop hook marks the
 *  end of the agent's genuine turn, so the reply-detection layer treats it
 *  as a reply-segment boundary (see classifyTopLevelMessages). */
export function isStopHookFeedback(content: string): boolean {
  return content.startsWith("Stop hook feedback:\n");
}

export function isCollapsibleUserMessage(content: string): { label: string } | null {
  if (isStopHookFeedback(content)) {
    return { label: "Stop hook feedback" };
  }
  if (content.startsWith("Base directory for this skill:")) {
    const match = content.match(/skills\/([^\n/]+)/);
    return { label: match ? `Skill: ${match[1]}` : "Skill expansion" };
  }
  return null;
}

export function isSkillExpansionUserMessage(content: string): boolean {
  return content.startsWith("Base directory for this skill:");
}

export function isHiddenUserMessage(content: string): boolean {
  // The minds desktop client seeds every new agent with "/welcome" as its
  // initial message so the welcome skill can produce a friendly greeting.
  // Claude Code expands that invocation into TWO transcript events:
  //   1. the invocation itself -- the session parser normalizes Claude Code's
  //      slash-command expansion (<command-name>/welcome</command-name> + args)
  //      back to the typed "/welcome" text (see _normalize_slash_command), so it
  //      arrives here as exactly "/welcome",
  //   2. the skill expansion, which starts with
  //      "Base directory for this skill: .../skills/welcome/..." and
  //      carries the SKILL.md body.
  // Hide both so the first visible turn is just the assistant's greeting.
  if (content.trim() === "/welcome") {
    return true;
  }
  // Other skill expansions are folded into the corresponding "Tool: Skill"
  // tool-call block (see buildToolResultsWithSkillExpansions) so they
  // don't need to render inline as a separate chip.
  if (isSkillExpansionUserMessage(content)) {
    return true;
  }
  return false;
}

/** The reserved latchkey host an agent POSTs to when asking the user to approve
 *  an action (see the latchkey skill). Short enough to survive the 200-char
 *  input_preview truncation. */
const PERMISSION_REQUEST_HOST = "latchkey-self.invalid/permission-requests";

/** A POST method flag in a latchkey/curl command's input preview. */
const PERMISSION_REQUEST_POST_RE = /-X\s*POST|--request\s*POST/i;

/** True when a tool call is an agent permission request: a POST to the reserved
 *  latchkey permission-requests host. Detected from the tool *input* alone, so a
 *  request is recognised the moment it is issued -- even while it is still
 *  pending with no result yet, which is exactly when the user most needs to see
 *  and act on it. (Contrast `parsePermissionRequest` in permission-card, which
 *  additionally needs a successful result to pull out the request id for the
 *  modal button.) */
export function isPermissionRequestCall(tc: ToolCall): boolean {
  const input = tc.input_preview || "";
  return input.includes(PERMISSION_REQUEST_HOST) && PERMISSION_REQUEST_POST_RE.test(input);
}

/** The outcome of a permission request, once it has been resolved:
 *   - "granted"/"denied": the user made a decision.
 *   - "error": the request could not be completed (e.g. the user's sign-in flow
 *     did not finish) -- not a decision, so it reads distinctly on the card. */
export type PermissionResolution = "granted" | "denied" | "error";

/** When a permission request is resolved, the app injects a plain user message
 *  into the agent's transcript announcing the outcome (see the latchkey handlers
 *  in mngr). The message carries no request id -- only the service display name
 *  (predefined) or the file path (file-sharing) and the literal verdict -- so the
 *  timeline walk correlates it to a request by order, and only the verdict is
 *  read here.
 *
 *  Recognised forms (anchored to the start so a normal user prompt that merely
 *  quotes one of these isn't misread):
 *   - "Your permission request for <service> was granted ..." -> granted
 *   - "Your permission request for <service> was denied ..." -> denied
 *   - "Your <access> file-sharing permission request for '<path>' was granted/denied ..."
 *   - "Your permission request for <service> could not be completed because the
 *      user's sign-in flow did not finish ..." -> error (the request didn't
 *      complete; it is not a user decision). */
export function parsePermissionResolution(content: string): PermissionResolution | null {
  if (/^Your\b.*\bpermission request for\b.*\bwas granted\b/.test(content)) {
    return "granted";
  }
  if (/^Your\b.*\bpermission request for\b.*\bwas denied\b/.test(content)) {
    return "denied";
  }
  if (/^Your\b.*\bpermission request for\b.*\bcould not be completed\b/.test(content)) {
    return "error";
  }
  return null;
}
