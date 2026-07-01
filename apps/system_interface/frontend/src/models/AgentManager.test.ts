import { describe, expect, it } from "vitest";

import { buildSessionTerminalUrl } from "./AgentManager";

/** Read back the repeated ``arg`` query params in order. */
function parseArgs(url: string): string[] {
  const query = url.split("?")[1] ?? "";
  return new URLSearchParams(query).getAll("arg");
}

describe("buildSessionTerminalUrl", () => {
  it("emits the positional args in ttyd dispatch order", () => {
    const url = buildSessionTerminalUrl("terminal-1", "term-abc", "/mngr/code", false);
    expect(url.startsWith("/service/terminal/?")).toBe(true);
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-1", "term-abc", "/mngr/code", ""]);
  });

  it("sets 'restore' as the final arg only on restore", () => {
    const fresh = buildSessionTerminalUrl("terminal-2", "term-xyz", "", false);
    const restored = buildSessionTerminalUrl("terminal-2", "term-xyz", "", true);
    expect(parseArgs(fresh)[5]).toBe("");
    expect(parseArgs(restored)[5]).toBe("restore");
  });

  it("percent-encodes special characters but round-trips the original values", () => {
    const url = buildSessionTerminalUrl("my term", "id", "/a b/c", false);
    // The raw query must not carry literal spaces...
    expect(url).not.toContain(" ");
    // ...but decoding recovers the exact session name and workdir.
    expect(parseArgs(url)).toEqual(["_", "session", "my term", "id", "/a b/c", ""]);
  });
});
