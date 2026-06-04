/**
 * Pure predicates that classify a user_message event's content.
 *
 * The transcript stream uses the user_message type for several things
 * other than a genuine human turn: skill expansions, stop-hook feedback,
 * /welcome-style command invocations, background-task notifications, the
 * output of local slash commands, and the post-compaction continuation
 * summary. Both the turn-grouping layer (deciding turn boundaries) and the
 * rendering layer (deciding inline chrome) need to ask the same questions
 * about a given user_message, which is why these live in their own module
 * rather than inside either layer.
 */

/** Strips ANSI SGR escape sequences (e.g. the bold/reset codes Claude Code
 *  wraps around values in `/model` output). The ESC byte is built at runtime
 *  so the pattern carries no literal control character. */
const ANSI_SGR_RE = new RegExp(`${String.fromCharCode(27)}\\[[0-9;]*m`, "g");

/**
 * True for user_message events that are NOT a genuine user prompt and must
 * not be treated as turn boundaries -- doing so splits one logical turn into
 * several visible turns and scatters the tasks across them.
 *
 * This is the boundary question, deliberately distinct from the *rendering*
 * question (classifyUserMessageForDisplay). A slash-command invocation and the
 * post-compaction summary both get special rendering yet ARE genuine turn
 * boundaries (the user really did prompt / the conversation really did break),
 * so they are absent here.
 */
export function isNonBoundaryUserMessage(content: string): boolean {
  return isHiddenUserMessage(content) || isInlineNotificationUserMessage(content);
}

/**
 * True for the system notifications that render as an inline chip woven into
 * the current turn rather than opening their own: stop-hook feedback,
 * background-task completions, and local slash-command output. These are
 * non-boundary (see isNonBoundaryUserMessage) but, unlike hidden messages,
 * stay visible.
 */
export function isInlineNotificationUserMessage(content: string): boolean {
  if (isStopHookFeedback(content) || getLocalCommandStdout(content) !== null) {
    return true;
  }
  // Background-command notifications render inline; a sub-agent completion is
  // hidden (already shown as its own card), so it is not an inline notification.
  return parseTaskNotification(content) !== null && !isSubagentTaskNotification(content);
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
  if (isCompactSummary(content)) {
    return { label: "Conversation compacted" };
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

/**
 * The post-compaction continuation summary. When the context window fills (or
 * the user runs /compact), Claude Code starts the next turn with a synthetic
 * user_message carrying a long prose summary of the prior conversation. It
 * opens with this exact preamble.
 */
export function isCompactSummary(content: string): boolean {
  return content.startsWith("This session is being continued from a previous conversation");
}

export function isHiddenUserMessage(content: string): boolean {
  // The minds desktop client seeds every new agent with "/welcome" as its
  // initial message so the welcome skill can produce a friendly greeting.
  // Claude Code expands that invocation into TWO transcript events:
  //   1. the invocation itself, whose content wraps "/welcome" in
  //      <command-name>.../</command-name> (plus a <command-message>...),
  //   2. the skill expansion, which starts with
  //      "Base directory for this skill: .../skills/welcome/..." and
  //      carries the SKILL.md body.
  // Hide both so the first visible turn is just the assistant's greeting.
  if (content.includes("<command-name>/welcome</command-name>")) {
    return true;
  }
  // Other skill expansions are folded into the corresponding "Tool: Skill"
  // tool-call block (see buildToolResultsWithSkillExpansions) so they
  // don't need to render inline as a separate chip.
  if (isSkillExpansionUserMessage(content)) {
    return true;
  }
  // A local slash command that produced no stdout (just the empty wrapper)
  // carries nothing to show.
  if (getLocalCommandStdout(content) === "") {
    return true;
  }
  // A finished sub-agent already renders as its own card (with a link to its
  // conversation) at the point it was launched, so its completion notification
  // would be a redundant duplicate. Background *command* notifications have no
  // such card and still render -- see isInlineNotificationUserMessage.
  if (isSubagentTaskNotification(content)) {
    return true;
  }
  return false;
}

export interface TaskNotificationInfo {
  /** completed | failed | killed | ... (as Claude Code reports it). */
  status: string;
  /** Human-readable one-liner, e.g. `Background command "..." completed (exit code 0)`. */
  summary: string;
}

/** Reads the inner text of the first `<tag>...</tag>` pair, or null. */
function readTag(content: string, tag: string): string | null {
  const match = content.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`));
  return match ? match[1] : null;
}

/**
 * Parse a `<task-notification>` user_message -- the event Claude Code injects
 * when a background command or sub-agent the agent launched finishes. Returns
 * the user-facing fields (status, summary), or null if the content is not a
 * task notification.
 */
export function parseTaskNotification(content: string): TaskNotificationInfo | null {
  if (!content.trimStart().startsWith("<task-notification>")) {
    return null;
  }
  const status = (readTag(content, "status") ?? "").trim();
  const summary = (readTag(content, "summary") ?? "").trim();
  return { status, summary };
}

/**
 * True when a task notification reports a finished *sub-agent* rather than a
 * background shell command. Claude Code formats the summary as
 * `Agent "<description>" <completed|was stopped>` for sub-agents and
 * `Background command "<description>" ...` for shell commands. Sub-agents are
 * rendered as their own card, so these notifications are hidden (see
 * isHiddenUserMessage); only background-command notifications surface.
 */
export function isSubagentTaskNotification(content: string): boolean {
  const info = parseTaskNotification(content);
  return info !== null && info.summary.startsWith('Agent "');
}

/** Reads the inner text of a `<local-command-stdout>` wrapper, ANSI-stripped
 *  and trimmed, or null if the content is not local-command output. May return
 *  the empty string when the command produced no stdout. */
export function getLocalCommandStdout(content: string): string | null {
  const inner = readTag(content, "local-command-stdout");
  if (inner === null) {
    return null;
  }
  return inner.replace(ANSI_SGR_RE, "").trim();
}

export interface SlashCommandInfo {
  /** The command including its leading slash, e.g. "/minds-dev-iterate". */
  name: string;
  /** The user's free-form argument text (their actual request), or "". */
  args: string;
}

/**
 * Parse a slash-command invocation. Claude Code emits the user typing
 * `/foo bar baz` as a user_message wrapping the pieces in
 * `<command-message>`, `<command-name>`, and `<command-args>` tags. The
 * `<command-args>` body is the user's real request, so these stay genuine turn
 * boundaries; this only pulls the pieces out of the XML for clean rendering.
 */
export function parseSlashCommandInvocation(content: string): SlashCommandInfo | null {
  const name = readTag(content, "command-name");
  if (name === null || name.trim() === "") {
    return null;
  }
  const args = readTag(content, "command-args");
  return { name: name.trim(), args: args !== null ? args.trim() : "" };
}

/** How a (non-hidden) user_message should render. The boundary question is
 *  answered separately (isNonBoundaryUserMessage); this is purely about chrome.
 *  Callers must check isHiddenUserMessage first -- hidden messages never reach
 *  here. */
export type UserMessageDisplay =
  | { kind: "task-notification"; status: string; summary: string }
  | { kind: "local-command"; text: string }
  | { kind: "compact-summary" }
  | { kind: "stop-hook" }
  | { kind: "slash-command"; name: string; args: string }
  | { kind: "plain" };

export function classifyUserMessageForDisplay(content: string): UserMessageDisplay {
  const task = parseTaskNotification(content);
  if (task !== null) {
    return { kind: "task-notification", status: task.status, summary: task.summary };
  }
  const stdout = getLocalCommandStdout(content);
  if (stdout !== null && stdout !== "") {
    return { kind: "local-command", text: stdout };
  }
  if (isCompactSummary(content)) {
    return { kind: "compact-summary" };
  }
  if (isStopHookFeedback(content)) {
    return { kind: "stop-hook" };
  }
  const slash = parseSlashCommandInvocation(content);
  if (slash !== null) {
    return { kind: "slash-command", name: slash.name, args: slash.args };
  }
  return { kind: "plain" };
}
