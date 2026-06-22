/**
 * Lazily fetches and caches the latchkey catalog info for a permission scope
 * (the human-readable service name + per-permission descriptions) from the
 * backend's `/api/latchkey/scopes/<scope>` proxy, so a permission-request card
 * can show the real service name instead of the raw scope.
 *
 * The first request for a scope kicks off a fetch and returns null; when the
 * fetch lands it caches the result and triggers a redraw so the card updates.
 * A scope the backend can't resolve (or a backend with no gateway, e.g. the dev
 * sandbox) caches null, so the card simply keeps showing the raw scope.
 */
import m from "mithril";
import { apiUrl } from "../base-path";

export interface PermissionInfo {
  name: string;
  description: string | null;
}

export interface ScopeInfo {
  scope: string;
  display_name: string;
  description: string | null;
  permissions: PermissionInfo[];
}

type CacheEntry = { state: "loading" } | { state: "ready"; info: ScopeInfo | null };

const cache = new Map<string, CacheEntry>();

/** The resolved scope info if it's loaded, else null. The first call for a
 *  scope starts a one-time background fetch that redraws when it resolves. */
export function getScopeInfo(scope: string): ScopeInfo | null {
  const cached = cache.get(scope);
  if (cached === undefined) {
    cache.set(scope, { state: "loading" });
    void fetchScopeInfo(scope);
    return null;
  }
  return cached.state === "ready" ? cached.info : null;
}

async function fetchScopeInfo(scope: string): Promise<void> {
  let info: ScopeInfo | null = null;
  try {
    const response = await fetch(apiUrl(`/api/latchkey/scopes/${encodeURIComponent(scope)}`));
    if (response.ok) {
      info = (await response.json()) as ScopeInfo;
    }
  } catch {
    info = null;
  }
  cache.set(scope, { state: "ready", info });
  m.redraw();
}

/** Seed the cache directly. Only for the dev-only visual mockup, which has no
 *  backend to fetch from. */
export function seedScopeInfo(info: ScopeInfo): void {
  cache.set(info.scope, { state: "ready", info });
}
