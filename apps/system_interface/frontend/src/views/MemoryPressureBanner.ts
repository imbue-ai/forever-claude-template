import m from "mithril";
import { apiUrl } from "../base-path";

// A calm, non-alarming strip that appears only while the workspace is under
// sustained memory pressure. It tells the user, plainly, what the watchdog has
// had to set aside so the interface stays responsive -- and disappears on its
// own once pressure clears.

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

function describeShed(items: RecentShedItem[]): string {
  const labelCounts = items
    .slice(0, 4)
    .map((item) => (item.count > 1 ? `${item.label} (x${item.count})` : item.label));
  if (labelCounts.length === 0) {
    return "";
  }
  return `Paused to free memory: ${labelCounts.join(", ")}.`;
}

export function MemoryPressureBanner(): m.Component {
  let timerId: number | undefined;

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
      const shedText = describeShed(currentStatus.recently_shed);
      const blocked = currentStatus.blocked_services;
      return m(
        "div",
        {
          class: "memory-pressure-banner",
          role: "status",
        },
        [
          m("span", { class: "memory-pressure-banner__title" }, "Memory is tight right now."),
          m(
            "span",
            { class: "memory-pressure-banner__detail" },
            " The workspace is freeing up memory to keep things responsive. Background work may be interrupted; your conversations are kept.",
          ),
          shedText ? m("span", { class: "memory-pressure-banner__detail" }, ` ${shedText}`) : null,
          blocked.length > 0
            ? m("span", { class: "memory-pressure-banner__detail" }, ` Paused service(s): ${blocked.join(", ")}.`)
            : null,
        ],
      );
    },
  };
}
