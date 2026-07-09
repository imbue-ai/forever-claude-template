import { describe, expect, it } from "vitest";
import { nextUserScrolledUp, isSelectionActiveWithin } from "./scrollFollow";

const base = { isClamp: false, wasUserScrolledUp: false };

describe("nextUserScrolledUp", () => {
  it("disengages following on any upward scroll, even within the bottom band", () => {
    // The core of the jitter bug: while streaming, the viewport sits within the
    // bottom band and a small upward scroll must stop tail-following so the next
    // redraw does not re-pin it to the bottom.
    expect(nextUserScrolledUp({ ...base, didScrollUp: true, isNearBottom: true, hasMoreAfter: false })).toBe(true);
  });

  it("disengages following on an upward scroll high above the bottom", () => {
    expect(nextUserScrolledUp({ ...base, didScrollUp: true, isNearBottom: false, hasMoreAfter: false })).toBe(true);
  });

  it("resumes following only at the true tail: near bottom with no newer history", () => {
    expect(nextUserScrolledUp({ ...base, didScrollUp: false, isNearBottom: true, hasMoreAfter: false })).toBe(false);
  });

  it("does not follow when scrolling down but still above the bottom band", () => {
    expect(nextUserScrolledUp({ ...base, didScrollUp: false, isNearBottom: false, hasMoreAfter: false })).toBe(true);
  });

  it("does not follow at the bottom of a jumped window that has newer history below", () => {
    // After an offset jump the window sits off the live tail, so newer events
    // remain unloaded below; being near that window's bottom is not the tail.
    expect(nextUserScrolledUp({ ...base, didScrollUp: false, isNearBottom: true, hasMoreAfter: true })).toBe(true);
  });

  it("keeps following through a shrink-clamp (does not read the clamp as scroll-up)", () => {
    // Eviction / a turn collapsing into one row shortens the content; the browser
    // pushes scrollTop up to the new max. didScrollUp looks true, but a follower
    // must keep following (the same redraw re-pins to the true tail).
    expect(
      nextUserScrolledUp({
        didScrollUp: true,
        isNearBottom: true,
        hasMoreAfter: false,
        isClamp: true,
        wasUserScrolledUp: false,
      }),
    ).toBe(false);
  });

  it("does not re-engage following for a scrolled-up reader on a shrink-clamp", () => {
    // A reader parked in history must not be yanked to the tail just because
    // content below them collapsed and the browser clamped scrollTop.
    expect(
      nextUserScrolledUp({
        didScrollUp: true,
        isNearBottom: true,
        hasMoreAfter: false,
        isClamp: true,
        wasUserScrolledUp: true,
      }),
    ).toBe(true);
  });
});

describe("isSelectionActiveWithin", () => {
  it("is inactive with no range", () => {
    expect(
      isSelectionActiveWithin({ hasRange: false, isCollapsed: true, anchorWithin: false, focusWithin: false }),
    ).toBe(false);
  });

  it("is inactive when collapsed (a bare caret)", () => {
    expect(isSelectionActiveWithin({ hasRange: true, isCollapsed: true, anchorWithin: true, focusWithin: true })).toBe(
      false,
    );
  });

  it("is inactive when neither endpoint is inside this view", () => {
    expect(
      isSelectionActiveWithin({ hasRange: true, isCollapsed: false, anchorWithin: false, focusWithin: false }),
    ).toBe(false);
  });

  it("is active when the anchor is inside this view (drag started here, dragged out)", () => {
    expect(
      isSelectionActiveWithin({ hasRange: true, isCollapsed: false, anchorWithin: true, focusWithin: false }),
    ).toBe(true);
  });

  it("is active when the focus is inside this view (drag started outside)", () => {
    expect(
      isSelectionActiveWithin({ hasRange: true, isCollapsed: false, anchorWithin: false, focusWithin: true }),
    ).toBe(true);
  });
});
