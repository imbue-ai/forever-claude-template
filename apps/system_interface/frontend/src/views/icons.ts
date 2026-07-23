// Single source of truth for every icon in the app.
//
// Icons are authored once here and consumed everywhere as SVG *strings*, which
// works for both rendering paths in this codebase: Mithril views wrap them with
// `m.trust(...)`, and the plain-DOM tab bar / lightbox assign them to
// `element.innerHTML`. Keeping icons as strings (rather than Mithril vnodes)
// is what lets a single definition serve both.
//
// The stroke-outline icons share one Feather/Lucide-style frame (24x24 grid,
// no fill, round-capped `currentColor` strokes); only their inner path markup
// differs, so it lives in STROKE_PATHS and `icon()` wraps it. Filled or
// otherwise non-standard glyphs (stop, warning, the Claude logo, the progress
// status badges, the login spinner) have their own builders below.

const XMLNS = "http://www.w3.org/2000/svg";

// Inner markup for stroke-outline icons, all drawn on a 24x24 grid.
const STROKE_PATHS = {
  attach:
    '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  // The single canonical "x" -- previously re-authored in four places (the
  // attachment-remove chip, the tab close button, the login modal, the image
  // lightbox).
  close: '<path d="M18 6L6 18"/><path d="M6 6l12 12"/>',
  file: '<path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/>',
  // Up-arrow, shared by the composer "send" button and the pending-message
  // "interrupt and send now" action.
  send: '<path d="M12 19V5"/><path d="M5 12l7-7 7 7"/>',
  trash:
    '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  share:
    '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
  refresh:
    '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  download:
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  // The single canonical checkmark, shared by the login "success" state and the
  // permission "granted" verdict.
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  // Exclamation mark used for the permission "couldn't complete" verdict.
  alert: '<path d="M12 6v7"/><path d="M12 17.5h0"/>',
  "chevron-down": '<path d="M6 9l6 6 6-6"/>',
  "chevron-right": '<path d="M9 6l6 6-6 6"/>',
  // Lightning bolt for the composer fast-mode toggle.
  zap: '<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>',
  "external-link":
    '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6"/>',
} as const;

export type IconName = keyof typeof STROKE_PATHS;

export interface IconOptions {
  // Pixel size. Omit to let CSS size the svg (used by the tab-bar buttons).
  size?: number;
  strokeWidth?: number;
  className?: string;
}

/** Full <svg> string for a stroke-outline icon. */
export function icon(name: IconName, opts: IconOptions = {}): string {
  const dims = opts.size === undefined ? "" : ` width="${opts.size}" height="${opts.size}"`;
  const cls = opts.className ? ` class="${opts.className}"` : "";
  const strokeWidth = opts.strokeWidth ?? 2;
  return (
    `<svg xmlns="${XMLNS}"${cls}${dims} viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${STROKE_PATHS[name]}</svg>`
  );
}

/** Solid square "stop / interrupt" glyph (filled, not stroked). */
export function stopIcon(size = 14): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="currentColor" stroke="none" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`
  );
}

/** Circular warning badge (outlined circle + exclamation). */
export function warningIcon(size = 26): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" aria-hidden="true">` +
    `<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>` +
    `<path d="M12 8v4.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>` +
    `<circle cx="12" cy="16" r="0.9" fill="currentColor"/></svg>`
  );
}

/** Animated login spinner (carries the `.claude-login-spinner` CSS hook). */
export function loginSpinnerIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="claude-login-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true">` +
    `<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-opacity="0.18" stroke-width="3"/>` +
    `<path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>`
  );
}

// The official Claude "burst" symbol. Source: Wikimedia Commons
// `File:Claude_AI_symbol.svg`, released under CC0 1.0 (public domain).
export function claudeLogoIcon(): string {
  return `<svg xmlns="${XMLNS}" class="claude-login-logo" viewBox="0 0 100 100" aria-hidden="true"><path d="m19.6 66.5 19.7-11 .3-1-.3-.5h-1l-3.3-.2-11.2-.3L14 53l-9.5-.5-2.4-.5L0 49l.2-1.5 2-1.3 2.9.2 6.3.5 9.5.6 6.9.4L38 49.1h1.6l.2-.7-.5-.4-.4-.4L29 41l-10.6-7-5.6-4.1-3-2-1.5-2-.6-4.2 2.7-3 3.7.3.9.2 3.7 2.9 8 6.1L37 36l1.5 1.2.6-.4.1-.3-.7-1.1L33 25l-6-10.4-2.7-4.3-.7-2.6c-.3-1-.4-2-.4-3l3-4.2L28 0l4.2.6L33.8 2l2.6 6 4.1 9.3L47 29.9l2 3.8 1 3.4.3 1h.7v-.5l.5-7.2 1-8.7 1-11.2.3-3.2 1.6-3.8 3-2L61 2.6l2 2.9-.3 1.8-1.1 7.7L59 27.1l-1.5 8.2h.9l1-1.1 4.1-5.4 6.9-8.6 3-3.5L77 13l2.3-1.8h4.3l3.1 4.7-1.4 4.9-4.4 5.6-3.7 4.7-5.3 7.1-3.2 5.7.3.4h.7l12-2.6 6.4-1.1 7.6-1.3 3.5 1.6.4 1.6-1.4 3.4-8.2 2-9.6 2-14.3 3.3-.2.1.2.3 6.4.6 2.8.2h6.8l12.6 1 3.3 2 1.9 2.7-.3 2-5.1 2.6-6.8-1.6-16-3.8-5.4-1.3h-.8v.4l4.6 4.5 8.3 7.5L89 80.1l.5 2.4-1.3 2-1.4-.2-9.2-7-3.6-3-8-6.8h-.5v.7l1.8 2.7 9.8 14.7.5 4.5-.7 1.4-2.6 1-2.7-.6-5.8-8-6-9-4.7-8.2-.5.4-2.9 30.2-1.3 1.5-3 1.2-2.5-2-1.4-3 1.4-6.2 1.6-8 1.3-6.4 1.2-7.9.7-2.6v-.2H49L43 72l-9 12.3-7.2 7.6-1.7.7-3-1.5.3-2.8L24 86l10-12.8 6-7.9 4-4.6-.1-.5h-.3L17.2 77.4l-4.7.6-2-2 .2-3 1-1 8-5.5Z"/></svg>`;
}

// ── Progress-view status badges (16x16 grid, carry their own `.pv-icon` hooks) ──

/** Completed step: filled disc with a white check. */
export function statusDoneIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="pv-icon pv-icon--done" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<circle cx="8" cy="8" r="7" fill="currentColor"/>` +
    `<path d="M4.5 8L7 10.5L11.5 6" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`
  );
}

/** Active-but-settled step: a static partial ring. */
export function statusRingIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="pv-icon pv-icon--in-flight" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" opacity="0.35"/>` +
    `<path d="M8 2 A6 6 0 0 1 14 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`
  );
}

/** Pending step: a dashed circle outline. */
export function statusPendingIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="pv-icon pv-icon--pending" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/></svg>`
  );
}
