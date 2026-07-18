import { describe, expect, it } from "vitest";
import {
  isHiddenUserMessage,
  isLocalCommandOutput,
  isModelOrEffortCommand,
  isNonBoundaryUserMessage,
} from "./message-classification";

describe("isModelOrEffortCommand — fast-retry control commands", () => {
  it("matches /model and /effort invocations with arguments", () => {
    expect(isModelOrEffortCommand("/model haiku")).toBe(true);
    expect(isModelOrEffortCommand("/effort low")).toBe(true);
    expect(isModelOrEffortCommand("/model opus")).toBe(true);
  });

  it("matches with surrounding whitespace", () => {
    expect(isModelOrEffortCommand("  /model haiku  ")).toBe(true);
  });

  it("does not match other slash commands or prose that mentions them", () => {
    expect(isModelOrEffortCommand("/models are great")).toBe(false);
    expect(isModelOrEffortCommand("/welcome")).toBe(false);
    expect(isModelOrEffortCommand("please use /model haiku")).toBe(false);
    expect(isModelOrEffortCommand("what model are you?")).toBe(false);
  });
});

describe("isLocalCommandOutput — echoed slash-command stdout", () => {
  it("matches a local-command-stdout wrapper", () => {
    expect(isLocalCommandOutput("<local-command-stdout>Set model to Haiku 4.5</local-command-stdout>")).toBe(true);
  });

  it("matches with leading whitespace", () => {
    expect(isLocalCommandOutput("\n  <local-command-stdout>done</local-command-stdout>")).toBe(true);
  });

  it("does not match ordinary user text", () => {
    expect(isLocalCommandOutput("Here is my <local-command-stdout> example")).toBe(false);
    expect(isLocalCommandOutput("hello")).toBe(false);
  });
});

describe("isHiddenUserMessage — hides fast-retry control chatter", () => {
  it("hides /model and /effort commands and their stdout echoes", () => {
    expect(isHiddenUserMessage("/model haiku")).toBe(true);
    expect(isHiddenUserMessage("/effort low")).toBe(true);
    expect(isHiddenUserMessage("<local-command-stdout>Set effort level to low</local-command-stdout>")).toBe(true);
  });

  it("still shows a genuine user prompt", () => {
    expect(isHiddenUserMessage("What is the capital of France?")).toBe(false);
  });

  it("keeps hiding the pre-existing cases (/welcome, skill expansions)", () => {
    expect(isHiddenUserMessage("/welcome")).toBe(true);
    expect(isHiddenUserMessage("Base directory for this skill: /x/skills/foo")).toBe(true);
  });

  it("treats the control chatter as a non-boundary message (does not split the turn)", () => {
    expect(isNonBoundaryUserMessage("/model haiku")).toBe(true);
    expect(isNonBoundaryUserMessage("<local-command-stdout>done</local-command-stdout>")).toBe(true);
  });
});
