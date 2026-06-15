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
      const height = element.offsetHeight;
      if (height > 0 && rowHeights.get(key) !== height) {
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
