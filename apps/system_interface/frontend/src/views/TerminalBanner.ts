/**
 * Dismissable banner shown above each terminal tab explaining the in-memory
 * lifecycle of these terminals.
 *
 * These terminals are backed by named tmux sessions on the same tmux server as
 * the agents. Their state lives only in memory: it survives closing the tab,
 * reloading the page, and even restarting the terminal (ttyd) service -- but it
 * does NOT survive a container/host restart, which tears the tmux server down.
 * The banner links to a doc on how to opt into on-disk persistence.
 *
 * Two controls:
 *  - "Dismiss" hides the banner for this tab instance only (this page load).
 *  - "Never show again" persists the choice server-side so the banner stays
 *    hidden across tabs and browsers.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

const PERSISTENCE_DOC_URL = "https://github.com/imbue-ai/mngr/blob/main/apps/minds/docs/persistent-terminals.md";

export function TerminalBanner(): m.Component {
  // Per-instance dismissal (this tab, this page load). The server-side "never
  // show again" flag is checked on mount and also flips this to hidden.
  let hidden = false;

  async function loadDismissedState(): Promise<void> {
    try {
      const response = await fetch(apiUrl("/api/terminals/banner-dismissed"));
      if (!response.ok) return;
      const data = (await response.json()) as { dismissed?: boolean };
      if (data.dismissed) {
        hidden = true;
        m.redraw();
      }
    } catch {
      // Best-effort: if we can't read the flag, show the banner (safe default).
    }
  }

  async function persistNeverShowAgain(): Promise<void> {
    hidden = true;
    m.redraw();
    try {
      await fetch(apiUrl("/api/terminals/banner-dismissed"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dismissed: true }),
      });
    } catch {
      // Best-effort: the banner is already hidden for this session either way.
    }
  }

  return {
    oninit() {
      loadDismissedState();
    },
    view() {
      if (hidden) return null;
      return m("div.terminal-banner", [
        m("span.terminal-banner-text", [
          "This terminal lives in memory. It survives closing the tab, reloading, and terminal-service restarts, but not a container restart. ",
          m(
            "a.terminal-banner-link",
            { href: PERSISTENCE_DOC_URL, target: "_blank", rel: "noopener noreferrer" },
            "Learn how to persist terminal state.",
          ),
        ]),
        m("span.terminal-banner-actions", [
          m(
            "button.terminal-banner-btn",
            {
              onclick: () => {
                hidden = true;
              },
            },
            "Dismiss",
          ),
          m(
            "button.terminal-banner-btn",
            {
              onclick: () => {
                void persistNeverShowAgain();
              },
            },
            "Never show again",
          ),
        ]),
      ]);
    },
  };
}
