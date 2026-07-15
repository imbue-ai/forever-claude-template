/**
 * Shared DOM measurement scaffolding for the virtualized message lists.
 *
 * Both the main chat panel and the subagent view render only a windowed slice of
 * their rows to the DOM and need the same glue: read each mounted row's height
 * (keyed by its DOM ``id``), cache it so the windowing math converges on real
 * heights, and schedule a single measure-then-redraw on the next animation frame.
 * That glue is identical between the two views, so it lives here; each view keeps
 * its own (divergent) scroll-anchoring, backfill and eviction logic.
 */

import m from "mithril";

// Pixels rendered above/below the viewport so scrolling does not flash blank
// before the next redraw fills the window.
export const OVERSCAN_PX = 800;
// Per-type fallback row heights, used until a row has been measured. Rough is
// fine: they only affect spacer sizing for off-screen rows, which is corrected
// as rows scroll into view and are measured.
export const ESTIMATED_USER_HEIGHT_PX = 90;
export const ESTIMATED_ASSISTANT_HEIGHT_PX = 240;

// Hysteresis for `measureRows`: a measured height must differ from the cached
// value by MORE than this many pixels to count as a real change. This breaks the
// measure->redraw->reflow->measure feedback loop that caused a continuous ~1px
// vertical jitter: a row sitting at a sub-pixel vertical offset would reflow by a
// fraction of a pixel each frame, and without a threshold that fraction was read
// as a "change", scheduling another redraw that shifted it again, forever. A
// sub-threshold spacer error for off-screen rows is harmless (it is corrected the
// moment the row genuinely changes height by more than the threshold), so
// ignoring these tiny deltas costs nothing and stops the loop. Genuine content
// changes (a streamed line is ~1.5x the font size) clear this threshold easily.
export const MEASURE_HYSTERESIS_PX = 1;

export interface RowMeasurer {
  /**
   * Read each rendered row's height from the DOM and cache it by its id, so the
   * window math and spacer sizes converge on real heights. Returns whether any
   * height changed (so the caller can schedule one more redraw to settle the
   * spacers).
   */
  measureRows(scrollEl: HTMLElement): boolean;
  /**
   * Measure on the next animation frame (debounced), redrawing once if any
   * height changed. Safe to call on every render/scroll.
   */
  scheduleMeasure(getScrollEl: () => HTMLElement | null): void;
  /** Measured height of the row with this key, or undefined if not yet measured. */
  getHeight(key: string): number | undefined;
  /**
   * Drop cached heights for keys no longer present once the cache drifts well
   * past the live row count, bounding its size as rows are evicted.
   */
  prune(keys: Set<string>): void;
  /** Forget all cached heights (e.g. when switching to a different agent). */
  reset(): void;
}

export function createRowMeasurer(): RowMeasurer {
  let rowHeights = new Map<string, number>();
  let measureScheduled = false;

  function measureRows(scrollEl: HTMLElement): boolean {
    const list = scrollEl.querySelector(".message-list");
    if (list === null) {
      return false;
    }
    let changed = false;
    for (const child of Array.from(list.children)) {
      const element = child as HTMLElement;
      const key = element.id;
      if (key === "") {
        continue; // spacer
      }
      // Sub-pixel, position-independent layout height. Unlike ``offsetHeight``
      // (integer, device-pixel-snapped and therefore dependent on the row's
      // fractional vertical position), this does not flip by 1px as the row
      // drifts a fraction of a pixel, which is the other half of the jitter fix.
      const height = element.getBoundingClientRect().height;
      if (height <= 0) {
        continue; // not laid out / hidden
      }
      const cached = rowHeights.get(key);
      // First measurement, or a change large enough to matter (see
      // MEASURE_HYSTERESIS_PX). Sub-threshold deltas are ignored entirely: we
      // neither update the cache nor report a change, so the cached height stays
      // anchored and the feedback loop cannot sustain itself.
      if (cached === undefined || Math.abs(height - cached) > MEASURE_HYSTERESIS_PX) {
        rowHeights.set(key, height);
        changed = true;
      }
    }
    return changed;
  }

  function scheduleMeasure(getScrollEl: () => HTMLElement | null): void {
    if (measureScheduled) {
      return;
    }
    measureScheduled = true;
    requestAnimationFrame(() => {
      measureScheduled = false;
      const scrollEl = getScrollEl();
      if (scrollEl !== null && measureRows(scrollEl)) {
        m.redraw();
      }
    });
  }

  function prune(keys: Set<string>): void {
    if (rowHeights.size <= keys.size + 256) {
      return;
    }
    for (const key of rowHeights.keys()) {
      if (!keys.has(key)) {
        rowHeights.delete(key);
      }
    }
  }

  return {
    measureRows,
    scheduleMeasure,
    getHeight: (key) => rowHeights.get(key),
    prune,
    reset: () => {
      rowHeights = new Map<string, number>();
    },
  };
}
