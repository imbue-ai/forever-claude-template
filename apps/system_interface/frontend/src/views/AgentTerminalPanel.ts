/**
 * Terminal tab bound to a specific agent.
 *
 * Opening an agent terminal attaches to that agent's tmux session, which does
 * not exist while the agent is STOPPED -- the ttyd dispatch's `tmux attach`
 * fails immediately. So before mounting the terminal iframe this panel POSTs
 * to the agent's start endpoint and waits for it to resolve. The backend
 * no-ops for already-running agents, so this is cheap in the common case.
 *
 * This covers both ways an agent terminal opens: the chat-page "Open agent
 * terminal" link and terminal tabs restored from a saved dockview layout
 * (both routed here by DockviewWorkspace.createComponent).
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { IframePanel } from "./IframePanel";

interface AgentTerminalPanelAttrs {
  agentId: string;
  url: string;
  title: string;
}

export function AgentTerminalPanel(): m.Component<AgentTerminalPanelAttrs> {
  let starting = true;
  let startError: string | null = null;

  async function ensureAgentStarted(agentId: string): Promise<void> {
    // Defensive: if the panel was constructed without an agentId (e.g. a
    // legacy or corrupt PanelParams entry from a restored layout), there is
    // no agent to start. POSTing to `/api/agents//start` would just 404;
    // skip straight to mounting the iframe with no error banner.
    if (agentId === "") {
      starting = false;
      m.redraw();
      return;
    }
    try {
      const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/start`), {
        method: "POST",
      });
      if (!response.ok) {
        const data = (await response.json().catch(() => ({}))) as { detail?: string };
        startError = data.detail ?? `Failed to start agent (HTTP ${response.status})`;
      }
    } catch (e) {
      startError = (e as Error).message;
    } finally {
      starting = false;
      m.redraw();
    }
  }

  return {
    oninit(vnode) {
      ensureAgentStarted(vnode.attrs.agentId);
    },

    view(vnode) {
      if (starting) {
        return m(
          "div",
          { class: "agent-terminal-starting flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "Starting agent..."),
        );
      }

      // Even if the start attempt errored, still mount the terminal iframe so
      // the user sees ttyd's own output; the error is surfaced just above it.
      if (startError !== null) {
        return m("div", { style: "display: flex; flex-direction: column; height: 100%;" }, [
          m(
            "div",
            {
              class: "agent-terminal-start-error text-red-500",
              style: "font-size: 0.85em; padding: 4px 8px; flex: 0 0 auto;",
            },
            `Could not start agent: ${startError}`,
          ),
          m(
            "div",
            { style: "flex: 1 1 auto; min-height: 0;" },
            m(IframePanel, { url: vnode.attrs.url, title: vnode.attrs.title }),
          ),
        ]);
      }

      return m(IframePanel, { url: vnode.attrs.url, title: vnode.attrs.title });
    },
  };
}
