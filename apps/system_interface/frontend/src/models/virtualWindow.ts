/**
 * Pure windowing math for the virtualized message list.
 *
 * Given the heights of an ordered list of rows and the current scroll position,
 * computes which contiguous slice of rows intersects the viewport (plus an
 * overscan margin) and how much vertical padding stands in for the rows above
 * and below that slice. Keeping this free of the DOM makes the non-trivial part
 * of virtualization unit-testable; the component only has to feed it measured
 * heights and render the result.
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
  let topPad = 0;
  let offset = 0;

  for (let i = 0; i < count; i++) {
    const height = getHeight(i);
    const rowTop = offset;
    const rowBottom = offset + height;

    // First row whose bottom edge crosses into the (over-scanned) viewport.
    if (startIndex === -1 && rowBottom > windowTop) {
      startIndex = i;
      topPad = rowTop;
    }
    // Track the last row whose top edge is still above the viewport bottom.
    if (rowTop < windowBottom) {
      endIndex = i + 1;
    }
    offset += height;
  }

  const totalHeight = offset;

  // The viewport is entirely below all content (scrolled past the end): render
  // the final row so the list is never blank, anchored at the bottom.
  if (startIndex === -1) {
    const lastHeight = getHeight(count - 1);
    return {
      startIndex: count - 1,
      endIndex: count,
      topPad: totalHeight - lastHeight,
      bottomPad: 0,
      totalHeight,
    };
  }

  // endIndex can lag startIndex when the viewport sits within a single tall row.
  if (endIndex <= startIndex) {
    endIndex = startIndex + 1;
  }

  let bottomPad = 0;
  for (let i = endIndex; i < count; i++) {
    bottomPad += getHeight(i);
  }

  return { startIndex, endIndex, topPad, bottomPad, totalHeight };
}
