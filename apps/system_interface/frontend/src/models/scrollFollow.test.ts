import { describe, expect, it } from "vitest";
import { isUserScrollUp, nextUserScrolledUp } from "./scrollFollow";

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

describe("isUserScrollUp", () => {
  it("reports a genuine upward scroll when the content height is unchanged", () => {
    expect(
      isUserScrollUp({ scrollTop: 100, previousScrollTop: 300, scrollHeight: 1000, previousScrollHeight: 1000 }),
    ).toBe(true);
  });

  it("ignores a downward scrollTop move caused by the content shrinking (browser clamp)", () => {
    // Pinned to the bottom (scrollTop 900 of 1000), a row measures shorter so
    // scrollHeight drops to 940 and the browser clamps scrollTop to 840. That is
    // not the user scrolling up, so following must not disengage.
    expect(
      isUserScrollUp({ scrollTop: 840, previousScrollTop: 900, scrollHeight: 940, previousScrollHeight: 1000 }),
    ).toBe(false);
  });

  it("reports an upward scroll even while the content grows", () => {
    expect(
      isUserScrollUp({ scrollTop: 200, previousScrollTop: 500, scrollHeight: 1200, previousScrollHeight: 1000 }),
    ).toBe(true);
  });

  it("is not an upward scroll when scrolling down", () => {
    expect(
      isUserScrollUp({ scrollTop: 500, previousScrollTop: 300, scrollHeight: 1000, previousScrollHeight: 1000 }),
    ).toBe(false);
  });
});
