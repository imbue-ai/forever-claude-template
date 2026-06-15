import { describe, expect, it } from "vitest";
import { nextUserScrolledUp } from "./scrollFollow";

describe("nextUserScrolledUp", () => {
  it("disengages following on any upward scroll, even within the bottom band", () => {
    // The core of the jitter bug: while streaming, the viewport sits within the
    // bottom band and a small upward scroll must stop tail-following so the next
    // redraw does not re-pin it to the bottom.
    expect(nextUserScrolledUp({ didScrollUp: true, isNearBottom: true, hasMoreAfter: false })).toBe(true);
  });

  it("disengages following on an upward scroll high above the bottom", () => {
    expect(nextUserScrolledUp({ didScrollUp: true, isNearBottom: false, hasMoreAfter: false })).toBe(true);
  });

  it("resumes following only at the true tail: near bottom with no newer history", () => {
    expect(nextUserScrolledUp({ didScrollUp: false, isNearBottom: true, hasMoreAfter: false })).toBe(false);
  });

  it("does not follow when scrolling down but still above the bottom band", () => {
    expect(nextUserScrolledUp({ didScrollUp: false, isNearBottom: false, hasMoreAfter: false })).toBe(true);
  });

  it("does not follow at the bottom of a jumped window that has newer history below", () => {
    // After an offset jump the window sits off the live tail, so newer events
    // remain unloaded below; being near that window's bottom is not the tail.
    expect(nextUserScrolledUp({ didScrollUp: false, isNearBottom: true, hasMoreAfter: true })).toBe(true);
  });
});
