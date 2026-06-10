import { describe, expect, it } from "vitest";
import {
  classifyUserMessageForDisplay,
  getLocalCommandStdout,
  isHiddenUserMessage,
  isInlineNotificationUserMessage,
  isNonBoundaryUserMessage,
  isSubagentTaskNotification,
  parseSlashCommandInvocation,
  parseTaskNotification,
} from "./user-message-classification";

// A real background-command notification (no <result>).
const BACKGROUND_COMMAND = `<task-notification>
<task-id>bfmxt7rtk</task-id>
<tool-use-id>toolu_01751zikjTwx4hsVZHawGNP2</tool-use-id>
<output-file>/tmp/claude-0/tasks/bfmxt7rtk.output</output-file>
<status>completed</status>
<summary>Background command "Re-arm background poll for worker" completed (exit code 0)</summary>
</task-notification>`;

// A real sub-agent notification, which carries a long <result> body.
const AGENT_NOTIFICATION = `<task-notification>
<task-id>afba53dcb5ec02e17</task-id>
<tool-use-id>toolu_014pLxw4nbN3vDptBp41P4xX</tool-use-id>
<output-file>/tmp/claude-501/tasks/afba53dcb5ec02e17.output</output-file>
<status>completed</status>
<summary>Agent "Verify conversation (incremental)" completed</summary>
<result>**Review Summary**

No issues found. The changes look good.</result>
</task-notification>`;

const FAILED_NOTIFICATION = `<task-notification>
<task-id>bx76xqgp0</task-id>
<tool-use-id>toolu_0195LRx2ZvZLkU3GECip71fq</tool-use-id>
<output-file>/tmp/out</output-file>
<status>failed</status>
<summary>Background command "Locate session paths" failed with exit code 1</summary>
</task-notification>`;

describe("parseTaskNotification", () => {
  it("extracts status and summary from a background-command notification", () => {
    const info = parseTaskNotification(BACKGROUND_COMMAND);
    expect(info).not.toBeNull();
    expect(info?.status).toBe("completed");
    expect(info?.summary).toBe('Background command "Re-arm background poll for worker" completed (exit code 0)');
  });

  it("captures a non-success status", () => {
    expect(parseTaskNotification(FAILED_NOTIFICATION)?.status).toBe("failed");
  });

  it("returns null for content that is not a task notification", () => {
    expect(parseTaskNotification("just a normal message")).toBeNull();
    expect(parseTaskNotification("talk about <task-notification> in prose")).toBeNull();
  });
});

describe("isSubagentTaskNotification", () => {
  it("distinguishes a finished sub-agent from a background shell command", () => {
    // Sub-agents already render as their own card, so their notifications are
    // hidden; background-command notifications are not.
    expect(isSubagentTaskNotification(AGENT_NOTIFICATION)).toBe(true);
    expect(isSubagentTaskNotification(BACKGROUND_COMMAND)).toBe(false);
    expect(isSubagentTaskNotification("a normal message")).toBe(false);
  });

  it("hides a sub-agent completion but keeps it a non-boundary, non-inline message", () => {
    expect(isHiddenUserMessage(AGENT_NOTIFICATION)).toBe(true);
    expect(isNonBoundaryUserMessage(AGENT_NOTIFICATION)).toBe(true);
    expect(isInlineNotificationUserMessage(AGENT_NOTIFICATION)).toBe(false);
    // A background-command notification stays visible and inline.
    expect(isHiddenUserMessage(BACKGROUND_COMMAND)).toBe(false);
    expect(isInlineNotificationUserMessage(BACKGROUND_COMMAND)).toBe(true);
  });
});

describe("getLocalCommandStdout", () => {
  it("returns the inner text of a local-command-stdout wrapper", () => {
    expect(getLocalCommandStdout("<local-command-stdout>Login successful</local-command-stdout>")).toBe(
      "Login successful",
    );
  });

  it("strips ANSI escape codes (e.g. from /model output)", () => {
    const esc = String.fromCharCode(27);
    const raw = `<local-command-stdout>Set model to ${esc}[1mOpus 4.8${esc}[22m and saved</local-command-stdout>`;
    expect(getLocalCommandStdout(raw)).toBe("Set model to Opus 4.8 and saved");
  });

  it("returns the empty string for an empty wrapper, and null for non-stdout content", () => {
    expect(getLocalCommandStdout("<local-command-stdout></local-command-stdout>")).toBe("");
    expect(getLocalCommandStdout("hello")).toBeNull();
  });
});

describe("parseSlashCommandInvocation", () => {
  it("pulls the command name and the user's args out of the XML wrapper", () => {
    const content =
      "<command-message>minds-dev-iterate</command-message>\n" +
      "<command-name>/minds-dev-iterate</command-name>\n" +
      "<command-args>start up electron and my docker mind</command-args>";
    const info = parseSlashCommandInvocation(content);
    expect(info).toEqual({ name: "/minds-dev-iterate", args: "start up electron and my docker mind" });
  });

  it("returns empty args when the command was invoked bare", () => {
    expect(parseSlashCommandInvocation("<command-name>/clear</command-name>")).toEqual({
      name: "/clear",
      args: "",
    });
  });

  it("returns null when there is no command-name tag", () => {
    expect(parseSlashCommandInvocation("plain text")).toBeNull();
  });
});

describe("boundary vs inline classification", () => {
  it("treats task notifications and local-command output as non-boundary inline notifications", () => {
    expect(isNonBoundaryUserMessage(BACKGROUND_COMMAND)).toBe(true);
    expect(isInlineNotificationUserMessage(BACKGROUND_COMMAND)).toBe(true);
    const stdout = "<local-command-stdout>Login successful</local-command-stdout>";
    expect(isNonBoundaryUserMessage(stdout)).toBe(true);
    expect(isInlineNotificationUserMessage(stdout)).toBe(true);
  });

  it("treats a slash-command invocation as a genuine turn boundary", () => {
    const content = "<command-name>/minds-dev-iterate</command-name>\n<command-args>do X</command-args>";
    expect(isNonBoundaryUserMessage(content)).toBe(false);
  });

  it("treats the post-compaction summary as a genuine turn boundary", () => {
    const content = "This session is being continued from a previous conversation that ran out of context...";
    expect(isNonBoundaryUserMessage(content)).toBe(false);
  });

  it("hides /welcome and a stdout-less local command", () => {
    expect(isHiddenUserMessage("<command-name>/welcome</command-name>")).toBe(true);
    expect(isHiddenUserMessage("<local-command-stdout></local-command-stdout>")).toBe(true);
  });
});

describe("classifyUserMessageForDisplay", () => {
  it("routes each injected format to its display kind", () => {
    expect(classifyUserMessageForDisplay(BACKGROUND_COMMAND).kind).toBe("task-notification");
    expect(classifyUserMessageForDisplay("<local-command-stdout>Login successful</local-command-stdout>").kind).toBe(
      "local-command",
    );
    expect(classifyUserMessageForDisplay("This session is being continued from a previous conversation...").kind).toBe(
      "compact-summary",
    );
    expect(classifyUserMessageForDisplay("Stop hook feedback:\nblocked").kind).toBe("stop-hook");
    expect(classifyUserMessageForDisplay("<command-name>/foo</command-name>").kind).toBe("slash-command");
    expect(classifyUserMessageForDisplay("a normal human message").kind).toBe("plain");
  });
});
