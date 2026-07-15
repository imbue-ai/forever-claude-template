/**
 * API client + pure helpers for named workspace layouts.
 *
 * Layouts are server-persisted dockview states addressed by slug; the
 * display name is free-form and the server owns slugification (a save posts
 * the display name and gets the slug back). See DockviewWorkspace for the
 * consuming logic.
 */

import { apiUrl } from "../base-path";
import type { DeviceKind } from "./ClientIdentity";

export interface LayoutInfo {
  slug: string;
  display_name: string;
  has_content: boolean;
}

export interface LayoutsListResponse {
  layouts: LayoutInfo[];
  last_active_slug: string | null;
}

/** Fetch the layout registry. Defensive: an unreachable server yields an
 *  empty list so the workspace still renders (nothing will persist). */
export async function fetchLayoutsList(): Promise<LayoutsListResponse> {
  try {
    const response = await fetch(apiUrl("/api/layouts"));
    if (!response.ok) return { layouts: [], last_active_slug: null };
    const data = (await response.json()) as { layouts?: LayoutInfo[]; last_active_slug?: string | null };
    return { layouts: data.layouts ?? [], last_active_slug: data.last_active_slug ?? null };
  } catch {
    return { layouts: [], last_active_slug: null };
  }
}

/** Fetch one layout's saved content. Returns null both for an empty layout
 *  (never saved yet -- render the fresh welcome-chat state) and on any
 *  fetch failure. */
export async function fetchLayoutContent(slug: string): Promise<unknown | null> {
  try {
    const response = await fetch(apiUrl(`/api/layouts/${encodeURIComponent(slug)}`));
    if (!response.ok) return null;
    const data = (await response.json()) as { layout?: unknown };
    return data.layout ?? null;
  } catch {
    return null;
  }
}

async function errorDetailFromResponse(response: Response): Promise<string> {
  const data = (await response.json().catch(() => ({}))) as { detail?: string };
  return data.detail ?? `HTTP ${response.status}`;
}

/** Autosave the active layout's content. Throws on failure (callers treat
 *  autosave as best-effort and catch). */
export async function autosaveLayout(slug: string, layoutPayload: unknown, clientId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/layouts/${encodeURIComponent(slug)}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ layout: layoutPayload, client_id: clientId }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
}

/** Save the given content under a display name (create or overwrite).
 *  Throws with the server's detail on rejection (bad name, slug conflict). */
export async function saveLayoutAs(
  displayName: string,
  layoutPayload: unknown,
  clientId: string,
): Promise<{ slug: string; display_name: string }> {
  const response = await fetch(apiUrl("/api/layouts"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName, layout: layoutPayload, client_id: clientId }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  return (await response.json()) as { slug: string; display_name: string };
}

/** Delete a named layout. Throws with the server's detail on rejection
 *  (unknown layout, last remaining layout). */
export async function deleteLayoutRequest(slug: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/layouts/${encodeURIComponent(slug)}/delete`), { method: "POST" });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
}

/**
 * Pick the layout a client should start on: its stored per-browser choice
 * when that layout still exists, else the user-agent default (mobile
 * browsers start on "mobile", everything else on "desktop") when that
 * exists, else the first layout. Null only when no layouts exist at all.
 */
export function chooseInitialLayout(
  layouts: LayoutInfo[],
  storedSlug: string,
  deviceKind: DeviceKind,
): LayoutInfo | null {
  if (layouts.length === 0) return null;
  const stored = layouts.find((layout) => layout.slug === storedSlug);
  if (stored) return stored;
  const preferredSlug = deviceKind === "mobile" ? "mobile" : "desktop";
  const preferred = layouts.find((layout) => layout.slug === preferredSlug);
  if (preferred) return preferred;
  return layouts[0];
}
