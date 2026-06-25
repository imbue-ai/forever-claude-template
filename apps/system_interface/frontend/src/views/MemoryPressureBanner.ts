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
  owning_agent_name?: string | null;
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

// The "Process" cell: the command, with a multiplier when several of the same
// kind were paused ("sleep ×3").
export function processName(item: RecentShedItem): string {
  return item.count > 1 ? `${item.label} ×${item.count}` : item.label;
}

// The "Creator" cell: who the paused work belonged to. A subprocess is
// attributed to its agent by name ("hogtest"); everything else falls back to a
// friendly description of what kind of thing it was.
export function creatorLabel(item: RecentShedItem): string {
  if (item.tier_rank === 8) {
    return item.owning_agent_name ?? "Agent";
  }
  return tierLabel(item.tier_rank);
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
          ? m("div", { class: "memory-pressure-banner__detail-panel" }, [
              m("table", { class: "memory-pressure-banner__table" }, [
                m("thead", [
                  m("tr", [
                    m("th", "Process"),
                    m("th", "Creator"),
                    m("th", { class: "memory-pressure-banner__col-freed" }, "Freed"),
                  ]),
                ]),
                m("tbody", [
                  ...items.map((item) =>
                    m("tr", [
                      m("td", { class: "memory-pressure-banner__cell-process" }, processName(item)),
                      m("td", creatorLabel(item)),
                      m(
                        "td",
                        { class: "memory-pressure-banner__col-freed" },
                        humanizeKb(item.reclaimed_kb),
                      ),
                    ]),
                  ),
                  ...blocked.map((service) =>
                    m("tr", [
                      m("td", { class: "memory-pressure-banner__cell-process" }, service),
                      m("td", "System service"),
                      m("td", { class: "memory-pressure-banner__col-freed" }, "—"),
                    ]),
                  ),
                ]),
              ]),
            ])
          : null,
      ]);
    },
  };
}
