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
  // This observation is a browser shrink-clamp, not a user scroll: the content
  // got shorter (eviction, a turn collapsing into one row) and the browser pushed
  // scrollTop up to the new maximum. Detected as scrollTop-decreased AND
  // scrollHeight-decreased AND now at the bottom. Such a move carries no user
  // intent, so the follow state must be preserved rather than re-derived from it.
  isClamp: boolean;
  // The current follow state (true == not following), preserved on a clamp.
  wasUserScrolledUp: boolean;
}

/**
 * Returns the next value of ``userScrolledUp`` (true == do not follow the tail).
 * Any upward movement disengages immediately; following resumes only at the true
 * tail (near the bottom with no newer history unloaded). A shrink-clamp is not
 * movement: it preserves the prior state, so a follower keeps following (the next
 * redraw re-pins to the true tail) and a scrolled-up reader is not yanked to the
 * tail just because content collapsed below them.
 */
export function nextUserScrolledUp(input: FollowStateInput): boolean {
  if (input.isClamp) {
    return input.wasUserScrolledUp;
  }
  if (input.didScrollUp) {
    return true;
  }
  return !(input.isNearBottom && !input.hasMoreAfter);
}

/**
 * Whether a live text selection should hold the transcript's virtualization and
 * eviction (so scrolling/streaming past the selected rows does not unmount them
 * and collapse the selection). DOM-free: the caller supplies the facts read from
 * ``document.getSelection()`` and a containment test against this view's scroll
 * element, so ChatPanel and SubagentView never react to each other's selections.
 */
export interface SelectionState {
  /** A selection object exists with at least one range. */
  hasRange: boolean;
  /** The selection is collapsed (a caret, no selected text). */
  isCollapsed: boolean;
  /** The anchor endpoint is inside this view's scroll element. */
  anchorWithin: boolean;
  /** The focus endpoint is inside this view's scroll element. */
  focusWithin: boolean;
}

export function isSelectionActiveWithin(state: SelectionState): boolean {
  // Either endpoint inside the view counts: a drag can start outside the panel
  // (anchor out, focus in) or be dragged out (anchor in, focus out), and in both
  // cases text inside this view is selected and must be protected.
  return state.hasRange && !state.isCollapsed && (state.anchorWithin || state.focusWithin);
}
