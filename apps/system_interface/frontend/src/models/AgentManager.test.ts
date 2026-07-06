import { describe, expect, it } from "vitest";

import { buildSessionTerminalUrl } from "./AgentManager";

/** Read back the repeated ``arg`` query params in order. */
function parseArgs(url: string): string[] {
  const query = url.split("?")[1] ?? "";
  return new URLSearchParams(query).getAll("arg");
}

describe("buildSessionTerminalUrl", () => {
  it("emits the positional args in ttyd dispatch order", () => {
    const url = buildSessionTerminalUrl("terminal-1", "term-abc", "/mngr/code");
    expect(url.startsWith("/service/terminal/?")).toBe(true);
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-1", "term-abc", "/mngr/code"]);
  });

  it("omits the working directory arg as empty when none is given", () => {
    const url = buildSessionTerminalUrl("terminal-2", "term-xyz", "");
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-2", "term-xyz", ""]);
  });

  it("percent-encodes special characters but round-trips the original values", () => {
    const url = buildSessionTerminalUrl("my term", "id", "/a b/c");
    // The raw query must not carry literal spaces...
    expect(url).not.toContain(" ");
    // ...but decoding recovers the exact session name and workdir.
    expect(parseArgs(url)).toEqual(["_", "session", "my term", "id", "/a b/c"]);
  });
});
