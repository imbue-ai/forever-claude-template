/**
 * Pure predicates that classify a user_message event's content.
 *
 * The transcript stream uses the user_message type for several things
 * other than a genuine human turn: skill expansions, stop-hook feedback,
 * /welcome-style command invocations, etc. Both the turn-grouping layer
 * (deciding turn boundaries) and the rendering layer (deciding inline
 * chrome) need to ask the same questions about a given user_message,
 * which is why these live in their own module rather than inside either
 * layer.
 */

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

export function isCollapsibleUserMessage(content: string): { label: string } | null {
  if (content.startsWith("Stop hook feedback:\n")) {
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
  return false;
}
