/**
 * Classification of transcript `user_message` content and tool calls, shared by
 * the turn-grouping layer (placement) and the rendering layer (appearance).
 *
 * The heart of this module is `classifyUserMessage`: it is the ONE place that
 * turns a raw user_message into a `UserMessageKind` (see message-kinds.ts for the
 * catalogue of kinds and exactly how each renders). Everything else -- grouping,
 * rendering -- keys off the returned kind and never re-sniffs the text.
 *
 * `classifyUserMessage` holds CLAUDE CODE's detector table. A different harness
 * would supply its own detectors mapping its markers to the same kinds; the
 * kinds, and everything downstream of them, are harness-agnostic.
 *
 * Permission REQUEST detection (a tool call) and permission RESOLUTION parsing
 * live here too but are separate concerns from the user_message kinds -- see
 * their own sections below.
 */

import type { ToolCall } from "../models/Response";
import { BROWSER_FLEET_TAG, KIND_SPEC, Rail, UserMessageKind } from "./message-kinds";

/** The result of classifying a user_message. */
export interface UserMessageClass {
  kind: UserMessageKind;
  /** Chip label for `SystemChip`; for `SkillExpansion` the skill name; else null. */
  label: string | null;
  /**
   * The text to DISPLAY, with any recognised wrapper sentinel stripped. For most
   * kinds this equals the original content; for a wrapped kind (e.g. a
   * browser-fleet nudge) it is the inner text without the tags.
   */
  body: string;
}

// --- Claude Code detectors --------------------------------------------------
// Each returns a UserMessageClass when it matches, else null. Order matters:
// `classifyUserMessage` runs them top-to-bottom and takes the first match.

/** The seeded `/welcome` invocation -- rendered as nothing (see isHidden note). */
function matchWelcome(content: string): UserMessageClass | null {
  // The minds desktop client seeds every new agent with "/welcome" so the
  // welcome skill produces a greeting; the invocation itself must not show.
  return content.trim() === "/welcome" ? { kind: UserMessageKind.Hidden, label: null, body: content } : null;
}

const SKILL_EXPANSION_PREFIX = "Base directory for this skill:";

/** A skill expansion -- its body is folded into the preceding `Tool: Skill`
 *  block (see buildToolResultsWithSkillExpansions), so it has no user-rail row. */
function matchSkillExpansion(content: string): UserMessageClass | null {
  if (!content.startsWith(SKILL_EXPANSION_PREFIX)) {
    return null;
  }
  const match = content.match(/skills\/([^\n/]+)/);
  return { kind: UserMessageKind.SkillExpansion, label: match ? match[1] : null, body: content };
}

const STOP_HOOK_PREFIX = "Stop hook feedback:\n";

/** Stop-hook feedback Claude Code injects when a Stop hook fires. */
function matchStopHook(content: string): UserMessageClass | null {
  return content.startsWith(STOP_HOOK_PREFIX)
    ? { kind: UserMessageKind.SystemChip, label: "Stop hook feedback", body: content }
    : null;
}

const TASK_NOTIFICATION_OPEN = "<task-notification>";
const TASK_NOTIFICATION_PREAMBLE = "[SYSTEM NOTIFICATION";

/** A background-task completion notice. Claude Code delivers this two ways: as a
 *  queued attachment (dropped in the backend parser, never reaches us) or -- the
 *  case handled here -- as a plain user line whose content carries a
 *  `<task-notification>` block, optionally behind a `[SYSTEM NOTIFICATION ...]`
 *  preamble. Either shape collapses to a chip rather than a bare bubble. */
function matchTaskNotification(content: string): UserMessageClass | null {
  const trimmed = content.trimStart();
  const isNotice =
    trimmed.startsWith(TASK_NOTIFICATION_OPEN) ||
    (trimmed.startsWith(TASK_NOTIFICATION_PREAMBLE) && content.includes(TASK_NOTIFICATION_OPEN));
  return isNotice ? { kind: UserMessageKind.SystemChip, label: "Background task", body: content } : null;
}

// Anchored, DOTALL-equivalent ([\s\S]) match of the fleet sentinel wrapping the
// whole message, so a nudge like "<agentic-browser-fleet>Browser foo-1 is
// free</agentic-browser-fleet>" is recognised and its inner text shown in the
// chip. We control this format (see BROWSER_FLEET_TAG), so an exact match is safe.
const BROWSER_FLEET_RE = new RegExp(`^\\s*<${BROWSER_FLEET_TAG}>([\\s\\S]*)</${BROWSER_FLEET_TAG}>\\s*$`);

/** A browser-fleet nudge (browser handed back / gone), wrapped by the fleet in
 *  the BROWSER_FLEET_TAG sentinel before it was sent via `mngr message`. */
function matchBrowserFleet(content: string): UserMessageClass | null {
  const m = content.match(BROWSER_FLEET_RE);
  return m ? { kind: UserMessageKind.SystemChip, label: "Browser fleet", body: m[1].trim() } : null;
}

/** Claude Code's detector table, most-specific first. */
const CLAUDE_USER_MESSAGE_DETECTORS: Array<(content: string) => UserMessageClass | null> = [
  matchWelcome,
  matchSkillExpansion,
  matchStopHook,
  matchTaskNotification,
  matchBrowserFleet,
];

/**
 * Classify a user_message into a `UserMessageKind` (+ chip label + display body).
 * This is the single classification entry point; grouping and rendering both call
 * it and act on the kind alone.
 *
 * Order of decision:
 *   1. An explicit detector matches (Stop hook, fleet, task-notification, skill,
 *      /welcome) -> that kind. Explicit detectors WIN over `isMeta` -- Stop-hook
 *      feedback is `isMeta` yet we deliberately surface it as a chip.
 *   2. else `isMeta` (Claude Code's flag for a framework-injected, model-only
 *      message: resume marker, image coordinate note, MCP-resource dumps, hook
 *      context, ...) -> Hidden. One rule hides the whole family, present and
 *      future, instead of a detector per message.
 *   3. else -> UserPrompt (a genuine human turn).
 */
export function classifyUserMessage(content: string, isMeta = false): UserMessageClass {
  for (const detect of CLAUDE_USER_MESSAGE_DETECTORS) {
    const result = detect(content);
    if (result !== null) {
      return result;
    }
  }
  if (isMeta) {
    return { kind: UserMessageKind.Hidden, label: null, body: content };
  }
  return { kind: UserMessageKind.UserPrompt, label: null, body: content };
}

// --- Thin semantic helpers over classifyUserMessage -------------------------
// Kept as named predicates because callers ask a specific structural question;
// all derive from the single classification above.

/**
 * True for a user_message that is NOT a genuine human turn and so must not be
 * treated as a turn boundary -- folding one of these into the running turn keeps
 * a single logical turn from being split into several visible ones. Covers the
 * collapsed system chips (Stop hook / fleet / task-notification), skill
 * expansions, and hidden messages alike.
 */
export function isNonBoundaryUserMessage(content: string, isMeta = false): boolean {
  // Derived from the KIND_SPEC registry (its `boundary` column) so the boundary
  // rule lives in exactly one place -- the spec -- rather than being duplicated
  // as a hardcoded kind check here.
  return !KIND_SPEC[classifyUserMessage(content, isMeta).kind].boundary;
}

/** True when the message folds into the current turn as a collapsed chip (rather
 *  than being dropped): the SystemChip kinds. This is the generalisation of the
 *  former stop-hook-only chip path -- fleet notices and task-notifications ride
 *  it now too. */
export function isSystemChipUserMessage(content: string): boolean {
  return classifyUserMessage(content).kind === UserMessageKind.SystemChip;
}

/** True when the content is a skill expansion (its body is folded into the
 *  preceding Skill tool-call block; see buildToolResultsWithSkillExpansions). */
export function isSkillExpansionUserMessage(content: string): boolean {
  return classifyUserMessage(content).kind === UserMessageKind.SkillExpansion;
}

/** True when the message produces NO row on the user rail -- either fully hidden
 *  (`/welcome`) or relocated into an assistant-side block (skill expansion). The
 *  rendering/rows layers use this to skip emitting a user-rail row. */
export function isHiddenUserMessage(content: string, isMeta = false): boolean {
  // "No user-rail row" is exactly "does not render on the User rail" -- read
  // straight from the KIND_SPEC registry. Covers both fully-hidden (/welcome,
  // is_meta) and relocated-to-assistant (skill expansions).
  return KIND_SPEC[classifyUserMessage(content, isMeta).kind].rail !== Rail.User;
}

// --- Permission REQUEST (a tool call) ---------------------------------------

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

// --- Permission RESOLUTION (a user_message verdict) -------------------------

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
 *  This is deliberately NOT part of classifyUserMessage: a resolution not only
 *  suppresses its own bubble but reaches back and mutates an earlier permission
 *  card (see UserMessageKind.PermissionResolution and the turn-grouping branch).
 *
 *  Recognised forms (anchored to the start so a normal user prompt that merely
 *  quotes one of these isn't misread), matching the messages the latchkey
 *  handlers in mngr actually inject (see
 *  apps/minds/imbue/minds/desktop_client/latchkey/handlers/ in the mngr repo):
 *   - predefined: "Your permission request for <service> was granted/denied ..."
 *   - file-sharing: "Your <access> file-sharing permission request for '<path>'
 *      was granted/denied ..."
 *   - workspace: "Your cross-workspace permission request was granted (<verbs>)
 *      for <target>." / "... was denied."
 *   - accounts: "Your request to list this device's signed-in accounts was
 *      granted/denied."
 *   - "Your permission request for <service> could not be completed because the
 *      user's sign-in flow did not finish ..." -> error (the request didn't
 *      complete; it is not a user decision).
 *
 *  The patterns require only "Your ... request ... was granted/denied" (not
 *  "permission request for"), because the exact phrasing differs per request
 *  type -- an unmatched resolution is worse than a loose match here: the
 *  request it should have resolved stays queued forever and every later
 *  verdict then lands on the wrong (one-older) card. Misreading a genuine
 *  user prompt stays unlikely: the pattern is anchored to the start and only
 *  consulted while a permission request is actually awaiting a decision. */
export function parsePermissionResolution(content: string): PermissionResolution | null {
  if (/^Your\b.*\brequest\b.*\bwas granted\b/.test(content)) {
    return "granted";
  }
  if (/^Your\b.*\brequest\b.*\bwas denied\b/.test(content)) {
    return "denied";
  }
  if (/^Your\b.*\brequest\b.*\bcould not be completed\b/.test(content)) {
    return "error";
  }
  return null;
}
