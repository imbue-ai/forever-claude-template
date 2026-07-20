// Pure decision for surfacing a highlighted agent's tab (e.g. the weekly
// Caretaker). Kept DOM-free and dependency-free so it can be unit-tested in
// isolation; DockviewWorkspace imports it and supplies the live inputs.

export type HighlightSurfaceDecision = "open" | "noop";

/**
 * Decide what to do with a highlighted agent on an agents_updated snapshot.
 *
 * ``previousKey`` is the agent's highlight key as last seen this session
 * (undefined for an agent that gained a highlight mid-session). The caller
 * handles the session baseline: the first snapshot with agents only records
 * keys and never asks for a decision, so a page reload does not re-open tabs
 * for runs that predate the page.
 *
 * - ``"open"`` -- the key changed (a new run) and the tab is closed: open it
 *   in the background, without stealing focus.
 * - ``"noop"`` -- the key is unchanged (no new run since last seen, including
 *   a tab the user closed) or the tab is already open.
 */
export function decideHighlightSurface(input: {
  currentKey: string;
  previousKey: string | undefined;
  isTabOpen: boolean;
}): HighlightSurfaceDecision {
  if (input.previousKey === input.currentKey) return "noop";
  return input.isTabOpen ? "noop" : "open";
}
