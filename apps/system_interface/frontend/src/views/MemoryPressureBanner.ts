import m from "mithril";
import { apiUrl } from "../base-path";

// A calm, non-alarming strip that appears only while the workspace is under
// sustained memory pressure. By default it shows the reassuring message plus a
// count of how much background work was paused; a chevron expands an itemized
// list (what was paused, which kind of work, how much memory it freed). It
// disappears on its own once pressure clears.

interface RecentShedItem {
  label: string;
  tier_rank: number;
  count: number;
  reclaimed_kb: number;
}

interface MemoryStatus {
  is_under_pressure: boolean;
  used_fraction: number;
  recently_shed: RecentShedItem[];
  blocked_services: string[];
}

const POLL_INTERVAL_MS = 5000;

let currentStatus: MemoryStatus | null = null;

async function pollMemoryStatus(): Promise<void> {
  try {
    const status = await m.request<MemoryStatus>({
      method: "GET",
      url: apiUrl("/api/memory-status"),
    });
    currentStatus = status;
  } catch {
    // The status endpoint is best-effort; on any failure leave the banner as
    // it was rather than flapping it. A genuinely-down interface has its own
    // recovery path.
    return;
  }
  m.redraw();
}

// Friendly names for the watchdog's shed tiers (only the sheddable tiers 5-8
// ever appear here). Keeps the expanded list readable without exposing the
// internal tier vocabulary.
const TIER_LABELS: Record<number, string> = {
  5: "Agent",
  6: "Background service",
  7: "Worker agent",
  8: "Agent subprocess",
};

export function tierLabel(tierRank: number): string {
  return TIER_LABELS[tierRank] ?? "Background process";
}

export function humanizeKb(kb: number): string {
  if (kb >= 1024 * 1024) return `${(kb / 1024 / 1024).toFixed(1)} GB`;
  if (kb >= 1024) return `${Math.round(kb / 1024)} MB`;
  return `${kb} KB`;
}

export function totalPausedCount(items: RecentShedItem[]): number {
  return items.reduce((sum, item) => sum + item.count, 0);
}

// Collapsed-state summary: a plain count of how many things were paused, or
// null when nothing was (the toggle is then hidden).
export function pausedSummary(items: RecentShedItem[]): string | null {
  const total = totalPausedCount(items);
  if (total <= 0) return null;
  return `${total} background ${total === 1 ? "task" : "tasks"} paused`;
}

export interface ShedItemDetail {
  name: string;
  meta: string;
}

export function shedItemDetail(item: RecentShedItem): ShedItemDetail {
  const name = item.count > 1 ? `${item.label} ×${item.count}` : item.label;
  const meta = `${tierLabel(item.tier_rank)} · ${humanizeKb(item.reclaimed_kb)} freed`;
  return { name, meta };
}

export function MemoryPressureBanner(): m.Component {
  let timerId: number | undefined;
  let expanded = false;

  return {
    oncreate() {
      void pollMemoryStatus();
      timerId = window.setInterval(() => void pollMemoryStatus(), POLL_INTERVAL_MS);
    },
    onremove() {
      if (timerId !== undefined) {
        window.clearInterval(timerId);
      }
    },
    view() {
      if (!currentStatus || !currentStatus.is_under_pressure) {
        return null;
      }
      const items = currentStatus.recently_shed;
      const blocked = currentStatus.blocked_services;
      const summary = pausedSummary(items);
      const hasDetails = items.length > 0 || blocked.length > 0;

      return m("div", { class: "memory-pressure-banner", role: "status" }, [
        m("div", { class: "memory-pressure-banner__main" }, [
          m("span", { class: "memory-pressure-banner__title" }, "The workspace is low on memory."),
          m(
            "span",
            { class: "memory-pressure-banner__detail" },
            " Background work may be paused to keep things responsive; your conversations and data are safe.",
          ),
          hasDetails
            ? m(
                "button",
                {
                  class: "memory-pressure-banner__toggle",
                  type: "button",
                  "aria-expanded": expanded ? "true" : "false",
                  onclick: () => {
                    expanded = !expanded;
                  },
                },
                [
                  m("span", summary ?? "Details"),
                  m(
                    "span",
                    {
                      class: "memory-pressure-banner__chevron",
                      "data-expanded": expanded ? "true" : "false",
                      "aria-hidden": "true",
                    },
                    "›",
                  ),
                ],
              )
            : null,
        ]),
        expanded && hasDetails
          ? m("ul", { class: "memory-pressure-banner__list" }, [
              ...items.map((item) => {
                const detail = shedItemDetail(item);
                return m("li", { class: "memory-pressure-banner__item" }, [
                  m("span", { class: "memory-pressure-banner__item-name" }, detail.name),
                  m("span", { class: "memory-pressure-banner__item-meta" }, detail.meta),
                ]);
              }),
              ...blocked.map((service) =>
                m("li", { class: "memory-pressure-banner__item" }, [
                  m("span", { class: "memory-pressure-banner__item-name" }, service),
                  m("span", { class: "memory-pressure-banner__item-meta" }, "Service paused"),
                ]),
              ),
            ])
          : null,
      ]);
    },
  };
}
