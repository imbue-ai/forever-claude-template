/**
 * Pure windowing math for the virtualized message list.
 *
 * `computeVisibleWindow` picks the contiguous slice of rows intersecting the
 * viewport (plus overscan). `computeTranscriptSlices` builds on it to produce the
 * ordered render segments -- spacers and row-runs -- including a *disjoint* run for
 * the rows holding a live text selection, so a selection scrolled far off-screen
 * keeps only its own rows mounted rather than everything between it and the
 * viewport. Keeping this free of the DOM makes the non-trivial part of
 * virtualization unit-testable; the views only feed it measured heights and render
 * the segments.
 */

export interface VirtualWindowInput {
  /** Number of rows in the list. */
  count: number;
  /** Height in pixels of row `index` (measured if known, else an estimate). */
  getHeight: (index: number) => number;
  /** Current scrollTop of the scroll container. */
  scrollTop: number;
  /** Visible height of the scroll container. */
  viewportHeight: number;
  /** Extra pixels rendered above and below the viewport to avoid blank flashes. */
  overscanPx: number;
}

export interface VirtualWindowResult {
  /** First row to render (inclusive). */
  startIndex: number;
  /** One past the last row to render (exclusive). */
  endIndex: number;
  /** Spacer height standing in for rows [0, startIndex). */
  topPad: number;
  /** Spacer height standing in for rows [endIndex, count). */
  bottomPad: number;
  /** Total height of all rows (topPad + rendered + bottomPad). */
  totalHeight: number;
}

/**
 * Compute the visible row window and the surrounding spacer heights.
 *
 * The window is the maximal contiguous run of rows whose vertical extent
 * overlaps `[scrollTop - overscanPx, scrollTop + viewportHeight + overscanPx]`.
 * When no row overlaps (e.g. an empty list) the window is empty and both pads
 * collapse so the spacers still sum to the true total height.
 */
export function computeVisibleWindow(input: VirtualWindowInput): VirtualWindowResult {
  const { count, getHeight, scrollTop, viewportHeight, overscanPx } = input;

  if (count <= 0) {
    return { startIndex: 0, endIndex: 0, topPad: 0, bottomPad: 0, totalHeight: 0 };
  }

  const windowTop = scrollTop - overscanPx;
  const windowBottom = scrollTop + viewportHeight + overscanPx;

  let startIndex = -1;
  let endIndex = 0;
  let offset = 0;

  for (let i = 0; i < count; i++) {
    const height = getHeight(i);
    const rowTop = offset;
    const rowBottom = offset + height;

    // First row whose bottom edge crosses into the (over-scanned) viewport.
    if (startIndex === -1 && rowBottom > windowTop) {
      startIndex = i;
    }
    // Track the last row whose top edge is still above the viewport bottom.
    if (rowTop < windowBottom) {
      endIndex = i + 1;
    }
    offset += height;
  }

  const totalHeight = offset;

  if (startIndex === -1) {
    // The viewport is entirely below all content (scrolled past the end, e.g. a
    // transient scrollTop overshoot while measured heights settle). Fill backward
    // from the last row until the viewport plus overscan is covered, instead of
    // rendering only the final row: a one-row window collapses scrollHeight for a
    // frame, the browser clamps scrollTop, and everything remounts next frame -- a
    // visible bounce (and it drops any selection). A full backward slice keeps the
    // rendered height stable across the overshoot.
    const coverage = viewportHeight + 2 * overscanPx;
    let filled = 0;
    startIndex = count - 1;
    for (let i = count - 1; i >= 0; i--) {
      startIndex = i;
      filled += getHeight(i);
      if (filled >= coverage) {
        break;
      }
    }
    endIndex = count;
  } else if (endIndex <= startIndex) {
    // endIndex can lag startIndex when the viewport sits within a single tall row.
    endIndex = startIndex + 1;
  }

  // Pads are the exact height sums of the rows the window excludes on each side,
  // so topPad + rendered + bottomPad always reconstructs the total height.
  let topPad = 0;
  for (let i = 0; i < startIndex; i++) {
    topPad += getHeight(i);
  }
  let bottomPad = 0;
  for (let i = endIndex; i < count; i++) {
    bottomPad += getHeight(i);
  }

  return { startIndex, endIndex, topPad, bottomPad, totalHeight };
}

/** A run of consecutive rows to render, `[startIndex, endIndex)` (end exclusive). */
export interface RowRunSegment {
  kind: "rows";
  startIndex: number;
  endIndex: number;
}

/** A vertical spacer standing in for the rows a run omits (or reserved history). */
export interface SpacerSegment {
  kind: "spacer";
  height: number;
}

export type WindowSegment = RowRunSegment | SpacerSegment;

export interface TranscriptSlicesInput {
  /** Number of rows in the list. */
  count: number;
  /** Height in pixels of row `index` (measured if known, else an estimate). */
  getHeight: (index: number) => number;
  /** Current scrollTop of the scroll container (raw, before phantom adjustment). */
  scrollTop: number;
  /** Visible height of the scroll container. */
  viewportHeight: number;
  /** Extra pixels rendered above and below the viewport to avoid blank flashes. */
  overscanPx: number;
  /**
   * Reserved height above/below the loaded rows for server history not yet loaded
   * (ChatPanel's phantom regions). Folded into the leading/trailing spacers so the
   * scrollbar reflects the whole conversation. Default 0 (the subagent view, which
   * loads its whole transcript). The viewport window math runs in the loaded rows'
   * own coordinate space, i.e. `scrollTop - phantomTopHeight`.
   */
  phantomTopHeight?: number;
  phantomBottomHeight?: number;
  /**
   * Inclusive row-index range holding a live text selection that must stay
   * rendered even when outside the viewport window, so scrolling or streaming past
   * it does not unmount its DOM and collapse the selection. Rendered as a *separate*
   * run (with a spacer between it and the viewport) when it is disjoint from the
   * viewport, so only its own rows mount -- not the arbitrarily many rows in
   * between. Clamped to `[0, count)` internally, so a stale range is safe.
   */
  pinnedRange?: { start: number; end: number } | null;
}

export interface TranscriptSlicesResult {
  /** Ordered segments to render: spacer, row-run, [spacer, row-run,] spacer. */
  segments: WindowSegment[];
  /** Total scroll height (phantomTop + all rows + phantomBottom). */
  totalHeight: number;
}

/**
 * Build the ordered render segments for the transcript: the viewport window, an
 * optional disjoint run for a pinned (selected) range, and the spacers between and
 * around them (with the phantom regions folded into the outer spacers).
 */
export function computeTranscriptSlices(input: TranscriptSlicesInput): TranscriptSlicesResult {
  const {
    count,
    getHeight,
    scrollTop,
    viewportHeight,
    overscanPx,
    phantomTopHeight = 0,
    phantomBottomHeight = 0,
    pinnedRange,
  } = input;

  const sumHeights = (from: number, to: number): number => {
    let sum = 0;
    for (let i = from; i < to; i++) {
      sum += getHeight(i);
    }
    return sum;
  };

  // Viewport window, computed in the loaded rows' own coordinate space (the loaded
  // rows sit below the top phantom spacer).
  const adjustedScrollTop = Math.max(0, scrollTop - phantomTopHeight);
  const viewport = computeVisibleWindow({
    count,
    getHeight,
    scrollTop: adjustedScrollTop,
    viewportHeight,
    overscanPx,
  });
  const totalHeight = phantomTopHeight + viewport.totalHeight + phantomBottomHeight;

  if (count <= 0) {
    return { segments: [{ kind: "spacer", height: totalHeight }], totalHeight };
  }

  // Resolve and clamp the pin to a valid, ordered, inclusive range.
  let pin: { start: number; end: number } | null = null;
  if (pinnedRange) {
    const a = Math.max(0, Math.min(pinnedRange.start, count - 1));
    const b = Math.max(0, Math.min(pinnedRange.end, count - 1));
    pin = { start: Math.min(a, b), end: Math.max(a, b) };
  }

  // Build the (1 or 2) row-runs, sorted and non-overlapping. `end` is exclusive.
  let runs: Array<{ start: number; end: number }>;
  if (pin === null) {
    runs = [{ start: viewport.startIndex, end: viewport.endIndex }];
  } else {
    const pinStart = pin.start;
    const pinEnd = pin.end + 1;
    // Overlapping or touching the viewport -> merge into one contiguous run.
    if (viewport.startIndex <= pinEnd && pinStart <= viewport.endIndex) {
      runs = [{ start: Math.min(viewport.startIndex, pinStart), end: Math.max(viewport.endIndex, pinEnd) }];
    } else if (pinEnd <= viewport.startIndex) {
      // Selection entirely above the viewport: its run, a gap spacer, the viewport.
      runs = [
        { start: pinStart, end: pinEnd },
        { start: viewport.startIndex, end: viewport.endIndex },
      ];
    } else {
      // Selection entirely below the viewport.
      runs = [
        { start: viewport.startIndex, end: viewport.endIndex },
        { start: pinStart, end: pinEnd },
      ];
    }
  }

  const segments: WindowSegment[] = [];
  segments.push({ kind: "spacer", height: phantomTopHeight + sumHeights(0, runs[0].start) });
  for (let r = 0; r < runs.length; r++) {
    segments.push({ kind: "rows", startIndex: runs[r].start, endIndex: runs[r].end });
    if (r < runs.length - 1) {
      segments.push({ kind: "spacer", height: sumHeights(runs[r].end, runs[r + 1].start) });
    }
  }
  segments.push({ kind: "spacer", height: sumHeights(runs[runs.length - 1].end, count) + phantomBottomHeight });

  return { segments, totalHeight };
}
