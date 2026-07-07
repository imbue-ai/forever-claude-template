/**
 * DOM glue for text-selection preservation, shared by the main chat panel and the
 * subagent view. Both virtualize their transcript into a windowed `.message-list`
 * and need to find which rows a live text selection touches, so the window can keep
 * those rows mounted (removing a selection endpoint's node collapses the
 * selection). Viewport stability under content-height changes is handled by native
 * scroll anchoring, not here.
 *
 * Every message row's root element carries a DOM `id` equal to its virtualization
 * key (see message-renderers / conversation-rows); spacers have an empty id.
 */

import { type SelectionState } from "../models/scrollFollow";

// Stop holding a selection's rows in the virtualization window once the viewport
// is more than this many rows away from them, so a selection left active during a
// long stream can't keep an unbounded span of rows mounted. Past this the pin is
// dropped (and the selection collapses) -- a deliberate memory bound; in practice
// users select-then-copy within seconds, far inside this gap.
export const SELECTION_PIN_MAX_GAP_ROWS = 300;

/** Walk up from a selection endpoint node to the message-row element (the child
 *  of `.message-list`) and return its key, or null if the node isn't inside a
 *  row. */
function rowKeyForNode(node: Node | null, listEl: Element): string | null {
  let current: Node | null = node;
  while (current !== null && current !== listEl) {
    const parent = current.parentNode;
    if (parent === listEl && current instanceof HTMLElement && current.id !== "") {
      return current.id;
    }
    current = parent;
  }
  return null;
}

/** Read the current selection's facts relative to this view's scroll element, for
 *  the pure `isSelectionActiveWithin` decision. */
export function selectionStateWithin(scrollEl: HTMLElement | null): SelectionState {
  const inactive: SelectionState = { hasRange: false, isCollapsed: true, anchorWithin: false, focusWithin: false };
  if (scrollEl === null) {
    return inactive;
  }
  const selection = document.getSelection();
  if (selection === null || selection.rangeCount === 0) {
    return inactive;
  }
  return {
    hasRange: true,
    isCollapsed: selection.isCollapsed,
    anchorWithin: selection.anchorNode !== null && scrollEl.contains(selection.anchorNode),
    focusWithin: selection.focusNode !== null && scrollEl.contains(selection.focusNode),
  };
}

/**
 * The inclusive row-index range spanned by the live selection's endpoints within
 * this view, or null when there is no active selection here or its endpoints
 * don't map to known rows (e.g. a selection anchored above `.message-list`, like
 * Cmd+A). Used to pin those rows into the virtualization window.
 */
export function resolveSelectionRowRange(
  scrollEl: HTMLElement | null,
  keyToIndex: Map<string, number>,
): { start: number; end: number } | null {
  if (scrollEl === null) {
    return null;
  }
  const selection = document.getSelection();
  if (selection === null || selection.rangeCount === 0 || selection.isCollapsed) {
    return null;
  }
  const list = scrollEl.querySelector(".message-list");
  if (list === null) {
    return null;
  }
  const indices: number[] = [];
  for (const node of [selection.anchorNode, selection.focusNode]) {
    if (node === null || !scrollEl.contains(node)) {
      continue;
    }
    const key = rowKeyForNode(node, list);
    if (key === null) {
      continue;
    }
    const index = keyToIndex.get(key);
    if (index !== undefined) {
      indices.push(index);
    }
  }
  if (indices.length === 0) {
    return null;
  }
  return { start: Math.min(...indices), end: Math.max(...indices) };
}
