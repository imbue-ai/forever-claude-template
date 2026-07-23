import { describe, expect, it } from "vitest";
import {
  classifyUserMessage,
  isHiddenUserMessage,
  isNonBoundaryUserMessage,
  isSkillExpansionUserMessage,
  isSystemChipUserMessage,
} from "./message-classification";
import { BROWSER_FLEET_TAG, UserMessageKind } from "./message-kinds";

describe("classifyUserMessage", () => {
  it("treats an ordinary human prompt as UserPrompt with the content as body", () => {
    const c = classifyUserMessage("please rebase onto main");
    expect(c.kind).toBe(UserMessageKind.UserPrompt);
    expect(c.body).toBe("please rebase onto main");
    expect(c.label).toBeNull();
  });

  it("classifies stop-hook feedback as a SystemChip", () => {
    const c = classifyUserMessage("Stop hook feedback:\nlint failed, fix it");
    expect(c.kind).toBe(UserMessageKind.SystemChip);
    expect(c.label).toBe("Stop hook feedback");
  });

  it("classifies a browser-fleet nudge as a SystemChip and strips the sentinel from the body", () => {
    const inner = "Browser foo-1 was handed back to you. Re-run `state foo-1`.";
    const c = classifyUserMessage(`<${BROWSER_FLEET_TAG}>${inner}</${BROWSER_FLEET_TAG}>`);
    expect(c.kind).toBe(UserMessageKind.SystemChip);
    expect(c.label).toBe("Browser fleet");
    expect(c.body).toBe(inner);
  });

  it("classifies a bare <task-notification> line as a SystemChip", () => {
    const c = classifyUserMessage("<task-notification>\n<status>completed</status>\n</task-notification>");
    expect(c.kind).toBe(UserMessageKind.SystemChip);
    expect(c.label).toBe("Background task");
  });

  it("classifies a task-notification behind a [SYSTEM NOTIFICATION] preamble as a SystemChip", () => {
    const c = classifyUserMessage(
      "[SYSTEM NOTIFICATION - NOT USER INPUT]\nblah\n<task-notification>x</task-notification>",
    );
    expect(c.kind).toBe(UserMessageKind.SystemChip);
    expect(c.label).toBe("Background task");
  });

  it("classifies a skill expansion and lifts the skill name as the label", () => {
    const c = classifyUserMessage(
      "Base directory for this skill: /home/.claude/skills/deep-research/\n\n# deep-research",
    );
    expect(c.kind).toBe(UserMessageKind.SkillExpansion);
    expect(c.label).toBe("deep-research");
  });

  it("classifies the seeded /welcome as Hidden", () => {
    expect(classifyUserMessage("/welcome").kind).toBe(UserMessageKind.Hidden);
  });

  it("hides an is_meta framework message (e.g. the image coordinate note) as Hidden", () => {
    const note =
      "[Image: original 1800x2800, displayed at 1286x2000. Multiply coordinates by 1.40 to map to original image.]";
    expect(classifyUserMessage(note, false).kind).toBe(UserMessageKind.UserPrompt); // without the flag it'd look like a human turn
    expect(classifyUserMessage(note, true).kind).toBe(UserMessageKind.Hidden); // the flag hides it
  });

  it("hides the resume-continuation marker via is_meta, not a bespoke matcher", () => {
    expect(classifyUserMessage("Continue from where you left off.", true).kind).toBe(UserMessageKind.Hidden);
    // A human who literally types the words (not is_meta) is still shown.
    expect(classifyUserMessage("Continue from where you left off.", false).kind).toBe(UserMessageKind.UserPrompt);
  });

  it("lets an explicit detector WIN over is_meta: Stop-hook feedback is is_meta yet shown as a chip", () => {
    // Stop-hook feedback is is_meta:true in the transcript, but we deliberately surface it.
    const c = classifyUserMessage("Stop hook feedback:\nlint failed", true);
    expect(c.kind).toBe(UserMessageKind.SystemChip);
    expect(c.label).toBe("Stop hook feedback");
  });

  it("does not misread a human message that merely mentions a marker", () => {
    // The marker must anchor at the start (or be a real tag), so quoting it in prose is safe.
    expect(classifyUserMessage("what does Stop hook feedback: mean?").kind).toBe(UserMessageKind.UserPrompt);
    expect(classifyUserMessage("tell me about <task-notification> handling").kind).toBe(UserMessageKind.UserPrompt);
  });

  it("hides the /model and /fast slash commands the composer picker/toggle send", () => {
    // The backend normalizes the transcript's <command-name> expansion back to
    // the typed command, which is what reaches the classifier.
    expect(classifyUserMessage("/model opus[1m]").kind).toBe(UserMessageKind.Hidden);
    expect(classifyUserMessage("/model sonnet").kind).toBe(UserMessageKind.Hidden);
    expect(classifyUserMessage("/fast on").kind).toBe(UserMessageKind.Hidden);
    expect(classifyUserMessage("/fast off").kind).toBe(UserMessageKind.Hidden);
    // Bare invocation (no args) is hidden too.
    expect(classifyUserMessage("/fast").kind).toBe(UserMessageKind.Hidden);
  });

  it("hides the <local-command-stdout> confirmation for /model and /fast", () => {
    expect(
      classifyUserMessage("<local-command-stdout>Set model to Opus 4.8 (1M context)</local-command-stdout>").kind,
    ).toBe(UserMessageKind.Hidden);
    expect(classifyUserMessage("<local-command-stdout>Fast mode ON</local-command-stdout>").kind).toBe(
      UserMessageKind.Hidden,
    );
  });

  it("does not hide a look-alike model/fast command or an unrelated local-command output", () => {
    // A different slash command, or a word that merely starts with model/fast, is a real turn.
    expect(classifyUserMessage("/models").kind).toBe(UserMessageKind.UserPrompt);
    expect(classifyUserMessage("model the data for me").kind).toBe(UserMessageKind.UserPrompt);
    // An unrelated local-command output is untouched (only /model, /fast outputs are hidden).
    expect(classifyUserMessage("<local-command-stdout>Total cost: $1.23</local-command-stdout>").kind).toBe(
      UserMessageKind.UserPrompt,
    );
  });
});

describe("semantic helpers", () => {
  it("isNonBoundaryUserMessage is true for every non-human kind", () => {
    expect(isNonBoundaryUserMessage("Stop hook feedback:\nx")).toBe(true);
    expect(isNonBoundaryUserMessage(`<${BROWSER_FLEET_TAG}>x</${BROWSER_FLEET_TAG}>`)).toBe(true);
    expect(isNonBoundaryUserMessage("<task-notification>x</task-notification>")).toBe(true);
    expect(isNonBoundaryUserMessage("Base directory for this skill: /x/skills/y/")).toBe(true);
    expect(isNonBoundaryUserMessage("/welcome")).toBe(true);
    expect(isNonBoundaryUserMessage("a normal message")).toBe(false);
  });

  it("isSystemChipUserMessage is true only for the collapsed-chip kinds", () => {
    expect(isSystemChipUserMessage("Stop hook feedback:\nx")).toBe(true);
    expect(isSystemChipUserMessage(`<${BROWSER_FLEET_TAG}>x</${BROWSER_FLEET_TAG}>`)).toBe(true);
    expect(isSystemChipUserMessage("<task-notification>x</task-notification>")).toBe(true);
    // skill expansion + welcome are non-boundary but NOT chips (no user-rail row)
    expect(isSystemChipUserMessage("Base directory for this skill: /x/skills/y/")).toBe(false);
    expect(isSystemChipUserMessage("/welcome")).toBe(false);
    expect(isSystemChipUserMessage("a normal message")).toBe(false);
  });

  it("isHiddenUserMessage covers /welcome and skill expansions (no user-rail row)", () => {
    expect(isHiddenUserMessage("/welcome")).toBe(true);
    expect(isHiddenUserMessage("Base directory for this skill: /x/skills/y/")).toBe(true);
    expect(isHiddenUserMessage("Stop hook feedback:\nx")).toBe(false);
    expect(isHiddenUserMessage("a normal message")).toBe(false);
  });

  it("isSkillExpansionUserMessage matches only skill expansions", () => {
    expect(isSkillExpansionUserMessage("Base directory for this skill: /x/skills/y/")).toBe(true);
    expect(isSkillExpansionUserMessage("/welcome")).toBe(false);
  });
});
