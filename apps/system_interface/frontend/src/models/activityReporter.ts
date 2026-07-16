/**
 * Reports workspace agent-tab activity to the backend so it can steer chat
 * agents' out-of-memory priority.
 *
 * The backend (`POST /api/activity`) re-tags each chat agent's `oom_score_adj`
 * from three signals -- whether its tab is open, whether it is visible, and how
 * recently it was messaged relative to other chats -- so a chat the user is
 * actively engaged with is less likely to be shed under memory pressure. This
 * module coalesces the many UI events that change those signals into a single
 * debounced POST.
 *
 * Presence (`open` / `visible`) is a full snapshot the caller recomputes and
 * reports whenever tabs change; the reporter retains the last one so a
 * `reportMessaged` recency bump carries the current presence with it. Every send
 * is best-effort: a dropped report just leaves the last-known priority in place
 * until the next report corrects it.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface ActivitySnapshot {
  /** Agent ids of every open chat tab. */
  open: string[];
  /** Agent ids of the chat tabs currently visible (subset of `open`). */
  visible: string[];
}

// Coalesce bursts (e.g. a layout restore that opens many tabs, or a rapid tab
// switch) into one POST. Short enough that the OOM priority tracks engagement
// closely; long enough to collapse a storm into a single request.
const DEBOUNCE_MS = 250;

let timer: ReturnType<typeof setTimeout> | null = null;
let latestOpen: string[] = [];
let latestVisible: string[] = [];
// The chat messaged since the last flush, carried into the next POST so the
// backend bumps its recency. Null when the pending report is presence-only.
let pendingMessaged: string | null = null;

function scheduleFlush(): void {
  if (timer !== null) {
    clearTimeout(timer);
  }
  timer = setTimeout(flush, DEBOUNCE_MS);
}

function flush(): void {
  timer = null;
  const messaged = pendingMessaged;
  pendingMessaged = null;
  void m
    .request({
      method: "POST",
      url: apiUrl("/api/activity"),
      body: { open: latestOpen, visible: latestVisible, messaged },
    })
    .catch(() => {
      // Best-effort: a failed report leaves the last-applied priority in place;
      // the next presence/recency report reconciles it.
    });
}

/** Report the current set of open/visible chat tabs (presence). */
export function reportActivity(snapshot: ActivitySnapshot): void {
  latestOpen = snapshot.open;
  latestVisible = snapshot.visible;
  scheduleFlush();
}

/** Report that `agentId` was just messaged, bumping its recency. Reuses the last
 *  reported presence, so it does not need the caller to recompute the tab sets. */
export function reportMessaged(agentId: string): void {
  pendingMessaged = agentId;
  scheduleFlush();
}
