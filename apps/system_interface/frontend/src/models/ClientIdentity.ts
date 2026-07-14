/**
 * Per-browser client identity for named-layout support.
 *
 * Each browser gets a stable uuid (minted once, kept in localStorage), a
 * device kind derived from the user agent (mobile vs desktop), and an active
 * named layout (also persisted per browser so reconnects restore the same
 * layout). The identity travels with every chat message and with the
 * WebSocket `client_state` registration, so the server (and agents, via
 * `layout.py context`) can attribute requests to a client and its layout.
 */

const CLIENT_ID_STORAGE_KEY = "si-client-id";
const ACTIVE_LAYOUT_STORAGE_KEY = "si-active-layout-slug";

export type DeviceKind = "mobile" | "desktop";

/** Pure UA classifier, separated from the navigator read for unit testing. */
export function classifyDeviceKind(userAgentDataMobile: boolean | undefined, userAgent: string): DeviceKind {
  if (userAgentDataMobile !== undefined) {
    return userAgentDataMobile ? "mobile" : "desktop";
  }
  return /Mobi|Android|iPhone|iPad|iPod/i.test(userAgent) ? "mobile" : "desktop";
}

export function getDeviceKind(): DeviceKind {
  // navigator.userAgentData is Chromium-only, hence the UA-string fallback.
  const uaData = (navigator as { userAgentData?: { mobile?: boolean } }).userAgentData;
  return classifyDeviceKind(uaData?.mobile, navigator.userAgent);
}

let cachedClientId: string | null = null;

export function getClientId(): string {
  if (cachedClientId !== null) {
    return cachedClientId;
  }
  const stored = localStorage.getItem(CLIENT_ID_STORAGE_KEY);
  if (stored) {
    cachedClientId = stored;
    return stored;
  }
  const minted =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `client-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  localStorage.setItem(CLIENT_ID_STORAGE_KEY, minted);
  cachedClientId = minted;
  return minted;
}

// The active layout slug. Held in module state (source of truth while the
// page lives) and mirrored to localStorage so the same browser restores the
// same layout on its next connect. Empty string means "not chosen yet"
// (during startup, before the layouts list has been fetched).
let activeLayoutSlug = "";

export function getStoredLayoutSlug(): string {
  return localStorage.getItem(ACTIVE_LAYOUT_STORAGE_KEY) ?? "";
}

export function getActiveLayoutSlug(): string {
  return activeLayoutSlug;
}

export function setActiveLayoutSlug(slug: string): void {
  activeLayoutSlug = slug;
  localStorage.setItem(ACTIVE_LAYOUT_STORAGE_KEY, slug);
}
