import { describe, expect, it } from "vitest";

import { decideHighlightSurface } from "./highlightSurface";

describe("decideHighlightSurface", () => {
  it("opens a closed tab when the key changes (a new run, e.g. after a wake reconnect)", () => {
    // The pre-sleep key is in module state; the post-reconnect snapshot
    // carries the overnight run's bumped key.
    expect(decideHighlightSurface({ currentKey: "new", previousKey: "old", isTabOpen: false })).toBe("open");
  });

  it("opens for an agent that gained a highlight mid-session", () => {
    expect(decideHighlightSurface({ currentKey: "k", previousKey: undefined, isTabOpen: false })).toBe("open");
  });

  it("leaves an already-open tab alone when the key changes", () => {
    expect(decideHighlightSurface({ currentKey: "new", previousKey: "old", isTabOpen: true })).toBe("noop");
  });

  it("does nothing while the key is unchanged (a closed tab stays closed)", () => {
    expect(decideHighlightSurface({ currentKey: "k", previousKey: "k", isTabOpen: false })).toBe("noop");
    expect(decideHighlightSurface({ currentKey: "k", previousKey: "k", isTabOpen: true })).toBe("noop");
  });
});
