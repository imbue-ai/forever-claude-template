/**
 * Pure decision for whether a virtualized transcript should follow the live tail
 * (auto-scroll to the bottom on each redraw) or stay put.
 *
 * It keys off scroll *direction*, not position: deciding purely from position
 * re-arms following whenever the viewport sits within the bottom band, so a
 * streaming redraw yanks a small upward scroll back down before it can clear the
 * band -- the "can't scroll up while streaming" jitter. DOM-free so it is
 * unit-testable.
 */

export interface FollowStateInput {
  didScrollUp: boolean;
  isNearBottom: boolean;
  // Newer history exists on the server but isn't loaded (only after a jump moved
  // the window off the live tail), so the bottom of the window isn't the tail.
  hasMoreAfter: boolean;
}

/**
 * Returns the next value of ``userScrolledUp`` (true == do not follow the tail).
 * Any upward movement disengages immediately; following resumes only at the true
 * tail (near the bottom with no newer history unloaded).
 */
export function nextUserScrolledUp(input: FollowStateInput): boolean {
  if (input.didScrollUp) {
    return true;
  }
  return !(input.isNearBottom && !input.hasMoreAfter);
}

export interface ScrollDirectionSample {
  scrollTop: number;
  previousScrollTop: number;
  scrollHeight: number;
  previousScrollHeight: number;
}

/**
 * Whether a scroll event is a genuine user upward scroll rather than the browser
 * clamping scrollTop down to fit a content height that just shrank.
 *
 * While following the live tail, scrollTop is pinned to the bottom. When async row
 * measurement settles a row shorter than its estimate, scrollHeight shrinks and the
 * browser clamps scrollTop down to ``scrollHeight - clientHeight``; that emits a
 * scroll event whose scrollTop is *lower* than before, which a naive ``scrollTop <
 * previousScrollTop`` reads as the user scrolling up -- latching userScrolledUp and
 * permanently disengaging tail-following. A clamp only happens when the content
 * shrank, so a downward scrollTop move that coincides with a shrunk scrollHeight is
 * never counted as user intent.
 */
export function isUserScrollUp(sample: ScrollDirectionSample): boolean {
  const heightShrank = sample.scrollHeight < sample.previousScrollHeight;
  return !heightShrank && sample.scrollTop < sample.previousScrollTop;
}
