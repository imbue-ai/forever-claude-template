/**
 * Tiny formatting helpers shared by the harness caption peers (claudeCaption /
 * codexCaption) for the TOOL_RUNNING activity label.
 */

export const MAX_TARGET_LEN = 60;

/** Last path segment of a "/"- or "\"-separated path. */
export function basename(p: string): string {
  const idx = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return idx >= 0 ? p.slice(idx + 1) : p;
}

/** Collapse whitespace and cap length with an ellipsis. */
export function shorten(s: string, max: number = MAX_TARGET_LEN): string {
  s = s.replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}
