import { describe, expect, it } from "vitest";
import { isHiddenUserMessage } from "./message-classification";

describe("isHiddenUserMessage", () => {
  it("hides the chat and Caretaker seed commands so the first visible turn is the greeting", () => {
    // The desktop client / scheduler seed agents with these slash commands; the
    // user should never see the trigger, only the assistant's greeting. The
    // persistent Caretaker is seeded with "/caretaker" on every run.
    expect(isHiddenUserMessage("/welcome")).toBe(true);
    expect(isHiddenUserMessage("/caretaker")).toBe(true);
  });

  it("does not hide the retired /caretaker-welcome command", () => {
    // /caretaker-welcome no longer exists -- the idempotent /caretaker skill handles
    // the welcome itself -- so it is treated as an ordinary message, not a seed.
    expect(isHiddenUserMessage("/caretaker-welcome")).toBe(false);
  });

  it("tolerates surrounding whitespace on the seed commands", () => {
    expect(isHiddenUserMessage("  /caretaker  \n")).toBe(true);
  });

  it("hides skill-expansion messages", () => {
    expect(isHiddenUserMessage("Base directory for this skill: /x/skills/caretaker/SKILL.md")).toBe(true);
  });

  it("does not hide a genuine user prompt that merely mentions a command", () => {
    expect(isHiddenUserMessage("can you run /caretaker for me?")).toBe(false);
    expect(isHiddenUserMessage("hello there")).toBe(false);
  });
});
