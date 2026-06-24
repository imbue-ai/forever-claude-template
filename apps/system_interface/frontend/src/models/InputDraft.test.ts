import { describe, expect, it } from "vitest";

// Mithril captures `requestAnimationFrame` at import time to schedule redraws;
// the node test env has no such global, so setInputDraft's `m.redraw()` would
// throw without this polyfill (mirrors PendingMessages.test.ts).
import { vi } from "vitest";
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { setInputDraft, consumeInputDraft } from "./InputDraft";

describe("InputDraft", () => {
  it("returns null when nothing is queued for the agent", () => {
    expect(consumeInputDraft("agent-none")).toBeNull();
  });

  it("hands back a queued draft exactly once", () => {
    setInputDraft("agent-1", "Suggest a few things I could work on.");
    expect(consumeInputDraft("agent-1")).toBe("Suggest a few things I could work on.");
    // Consumed: a second read sees nothing.
    expect(consumeInputDraft("agent-1")).toBeNull();
  });

  it("treats an empty draft as a real value (focus + clear), distinct from null", () => {
    setInputDraft("agent-2", "");
    expect(consumeInputDraft("agent-2")).toBe("");
    expect(consumeInputDraft("agent-2")).toBeNull();
  });

  it("keeps drafts isolated per agent", () => {
    setInputDraft("agent-a", "alpha");
    setInputDraft("agent-b", "beta");
    expect(consumeInputDraft("agent-b")).toBe("beta");
    expect(consumeInputDraft("agent-a")).toBe("alpha");
  });

  it("lets a later draft overwrite an unconsumed earlier one", () => {
    setInputDraft("agent-3", "first");
    setInputDraft("agent-3", "second");
    expect(consumeInputDraft("agent-3")).toBe("second");
  });
});
