/**
 * Pure decision for whether a virtualized transcript should follow the live
 * tail (auto-scroll to the bottom on each redraw) or stay put because the user
 * scrolled up to read.
 *
 * The non-obvious part is that the decision must key off scroll *direction*, not
 * just position. Auto-following re-pins the viewport to the bottom on every
 * redraw, and redraws fire continuously while a turn streams. If "follow" were
 * decided purely from position, then any moment the viewport sits within the
 * bottom threshold band would re-arm following, and the next streaming redraw
 * would yank a small upward scroll back down before the user could escape the
 * band -- the "can't scroll up, screen jitters" bug. Treating any user-initiated
 * upward movement as an immediate disengage fixes that: a single pixel of
 * scroll-up stops the follow, and following only resumes once the user scrolls
 * back down to the true tail. Keeping this DOM-free makes it unit-testable; the
 * component feeds it the measured direction and edge state.
 */

export interface FollowStateInput {
  /** The user moved the viewport upward since the last observed scroll position. */
  didScrollUp: boolean;
  /** The viewport is within the bottom threshold of the loaded rows. */
  isNearBottom: boolean;
  /**
   * Newer history exists on the server but is not loaded (only possible after a
   * jump moved the window off the live tail). Being near the bottom of such a
   * window is not the live tail, so following must not resume there.
   */
  hasMoreAfter: boolean;
}

/**
 * Returns the next value of ``userScrolledUp`` (true == do not follow the tail).
 *
 * Any upward movement disengages immediately, even within the bottom band.
 * Otherwise following resumes only at the true tail: near the bottom of the
 * loaded rows with no newer history still unloaded.
 */
export function nextUserScrolledUp(input: FollowStateInput): boolean {
  if (input.didScrollUp) {
    return true;
  }
  return !(input.isNearBottom && !input.hasMoreAfter);
}
