/**
 * Chat panel for dockview. Contains the main message list and message input
 * for an agent, mounted as a tab within the dockview workspace.
 *
 * If the agent is still being created (a proto-agent), shows the creation
 * log stream instead. Automatically switches to the chat view when creation
 * completes.
 */

import m from "mithril";
import { isSlotClaimed } from "../slots";
import {
  fetchBackfillEvents,
  getEventsForAgent,
  getEnrichmentForAgent,
  getFirstEventId,
  isConversationNotFound,
  isBackfillComplete,
} from "../models/Response";
import { connectToStream, disconnectFromStream, loadSnapshotWithStream } from "../models/StreamingMessage";
import { getAgentById, getProtoAgents } from "../models/AgentManager";
import { getPendingMessages } from "../models/PendingMessages";
import { openLoginModal } from "../models/ClaudeAuth";
import { apiUrl } from "../base-path";
import { EmptySlot } from "./EmptySlot";
import { MessageInput } from "./MessageInput";
import {
  renderUserMessage,
  renderAssistantMessage,
  buildToolResultsWithSkillExpansions,
  computeAuthErrorHiddenEventIds,
} from "./message-renderers";
import { buildAgentTerminalUrl, getTerminalUrl, openIframeTabForAgent } from "./DockviewWorkspace";
import { buildSections } from "./turn-grouping";
import { ProgressBlock } from "./ProgressBlock";
import { ActivityIndicator } from "./ActivityIndicator";

function getAgentTerminalUrl(agentId: string): string {
  // The ttyd dispatch script is invoked as `bash -c "$SCRIPT" <args...>` where
  // the first trailing arg becomes $0 (not $1). ``buildAgentTerminalUrl``
  // emits ``arg=_&arg=agent&arg=<name>`` so the dispatch lands ``agent`` in
  // ``$1`` and the name in ``$2``, mirroring the workdir deep-link pattern.
  // When the agent isn't in the local cache yet, fall back to the bare
  // base URL and let agent.sh attach to the ambient session.
  const agent = getAgentById(agentId);
  if (!agent?.name) {
    const baseUrl = getTerminalUrl();
    const separator = baseUrl.includes("?") ? "&" : "?";
    return `${baseUrl}${separator}arg=_&arg=agent`;
  }
  return buildAgentTerminalUrl(agent.name);
}

function openAgentTerminalTab(agentId: string): void {
  const agent = getAgentById(agentId);
  const title = agent?.name ? `${agent.name} terminal` : "agent terminal";
  openIframeTabForAgent(agentId, getAgentTerminalUrl(agentId), title);
}

const SCROLL_BOTTOM_THRESHOLD_PX = 40;

// Layout for the centered message column. Shared between the normal transcript
// render and the empty-state branch that shows an optimistic first message, so
// the two stay visually identical.
const MESSAGE_LIST_CLASS = "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6";

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollToBottom(element: HTMLElement): void {
  element.scrollTop = element.scrollHeight;
}

function isProtoAgent(agentId: string): boolean {
  return getProtoAgents().some((p) => p.agent_id === agentId);
}

// Optimistic bubbles for messages the user just sent that have not yet been
// reconciled against a real transcript event. Rendered with the same renderer
// as real user turns so they are visually indistinguishable, and appended after
// the transcript so they sit at the bottom where the user expects their message.
function renderPendingMessages(agentId: string): m.Vnode[] {
  const nodes: m.Vnode[] = [];
  for (const pending of getPendingMessages(agentId)) {
    const node = renderUserMessage({
      type: "user_message",
      event_id: pending.id,
      content: pending.content,
      role: "user",
      source: "pending",
      timestamp: "",
    });
    if (node !== null) nodes.push(node);
  }
  return nodes;
}

export function ChatPanel(): m.Component<{ agentId: string }> {
  let loading = false;
  let loadingError: string | null = null;
  let currentAgentId: string | null = null;
  let userScrolledUp = false;
  let previousScrollTop = 0;
  let backfillStarted = false;

  // Snapshot-load path: SSE only carries events emitted after subscription,
  // so an auth-error that happened before the user opened the panel (e.g.
  // the auto-`/welcome` failing during fresh mind creation) wouldn't open
  // the modal otherwise. Walking back to the last assistant_message means
  // an already-recovered agent (whose history contains old auth errors
  // but has since produced healthy replies) does not open it on reload --
  // only an agent whose current state is broken does. The modal itself is
  // a single app-level instance driven by global auth state (see
  // models/ClaudeAuth.ts), so this just flips that shared flag.
  function checkLatestAssistantForAuthError(agentId: string): void {
    const events = getEventsForAgent(agentId);
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i];
      if (event.type === "assistant_message") {
        if (event.is_auth_error === true) {
          openLoginModal();
        }
        return;
      }
    }
  }

  // Screen capture state (shown when agent has no conversation)
  let screenContent: string | null = null;
  let screenError: string | null = null;
  let screenLoading = false;
  let screenAgentId: string | null = null;

  // Proto-agent log state
  let logWs: WebSocket | null = null;
  let logLines: string[] = [];
  let logDone = false;
  let logSuccess = false;
  let logError: string | null = null;
  let logAgentId: string | null = null;

  async function fetchScreenCapture(agentId: string): Promise<void> {
    if (screenAgentId === agentId && (screenContent !== null || screenLoading)) {
      return;
    }
    screenAgentId = agentId;
    screenLoading = true;
    screenContent = null;
    screenError = null;
    try {
      const result = await m.request<{ screen: string | null; error?: string }>({
        method: "GET",
        url: apiUrl("/api/agents/:agentId/screen"),
        params: { agentId, scrollback: "true" },
      });
      screenContent = result.screen;
      screenError = result.error ?? null;
    } catch {
      screenError = "Failed to capture screen";
    } finally {
      screenLoading = false;
      m.redraw();
    }
  }

  function connectLogWs(agentId: string): void {
    if (logWs !== null) {
      logWs.close();
    }
    logLines = [];
    logDone = false;
    logSuccess = false;
    logError = null;
    logAgentId = agentId;

    const base = apiUrl(`/api/proto-agents/${encodeURIComponent(agentId)}/logs`);
    const loc = window.location;
    const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
    let url: string;
    if (base.startsWith("http")) {
      url = base.replace(/^http/, "ws");
    } else {
      url = `${protocol}//${loc.host}${base}`;
    }

    logWs = new WebSocket(url);

    logWs.onmessage = (event: MessageEvent) => {
      const data = JSON.parse(event.data as string) as
        | { line: string }
        | { done: true; success: boolean; error: string | null };

      if ("line" in data) {
        logLines.push(data.line);
      } else if ("done" in data) {
        logDone = true;
        logSuccess = data.success;
        logError = data.error;
      }
      m.redraw();
    };

    logWs.onclose = () => {
      logWs = null;
    };

    logWs.onerror = () => {
      logWs?.close();
    };
  }

  function disconnectLogWs(): void {
    if (logWs !== null) {
      logWs.close();
      logWs = null;
    }
    logAgentId = null;
  }

  function renderBuildLog(agentId: string): m.Vnode {
    if (logAgentId !== agentId) {
      connectLogWs(agentId);
    }

    return m("div", { style: "display: flex; flex-direction: column; height: 100%; padding: 16px;" }, [
      m(
        "div",
        { style: "font-weight: 600; margin-bottom: 8px; font-size: 0.9em; color: #666;" },
        logDone ? (logSuccess ? "Agent created successfully" : "Agent creation failed") : "Creating agent...",
      ),
      logError ? m("div", { style: "color: red; margin-bottom: 8px; font-size: 0.85em;" }, logError) : null,
      m(
        "div",
        {
          style:
            "flex: 1; overflow-y: auto; background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 0.8em; padding: 12px; border-radius: 4px; white-space: pre-wrap; word-break: break-all;",
          onupdate(vnode: m.VnodeDOM) {
            const el = vnode.dom as HTMLElement;
            el.scrollTop = el.scrollHeight;
          },
        },
        logLines.map((line, i) => m("div", { key: i, style: "line-height: 1.5;" }, line)),
      ),
    ]);
  }

  async function loadAgent(agentId: string): Promise<void> {
    loading = true;
    loadingError = null;

    try {
      // Buffer SSE deltas arriving during the snapshot fetch so the wholesale
      // snapshot replace in fetchEvents cannot drop a live event on first load.
      await loadSnapshotWithStream(agentId);
      if (agentId === currentAgentId) {
        loading = false;
        loadingError = null;
        checkLatestAssistantForAuthError(agentId);
      }
    } catch (error) {
      if (agentId === currentAgentId) {
        loading = false;
        loadingError = (error as Error).message ?? String(error);
      }
    }
  }

  function manageStreamConnection(agentId: string): void {
    if (!isConversationNotFound(agentId)) {
      connectToStream(agentId);
    } else {
      disconnectFromStream(agentId);
    }
  }

  function ensureAgentLoaded(agentId: string): void {
    if (agentId === currentAgentId) {
      return;
    }

    currentAgentId = agentId;
    previousScrollTop = 0;
    userScrolledUp = false;
    backfillStarted = false;
    loadAgent(agentId);
  }

  async function runBackfillLoop(agentId: string): Promise<void> {
    const MAX_STALLED_RETRIES = 5;
    const BACKOFF_BASE_MS = 1000;
    const BACKOFF_CAP_MS = 30000;
    let stalledCount = 0;

    while (!isBackfillComplete(agentId) && agentId === currentAgentId) {
      const firstIdBefore = getFirstEventId(agentId);
      await fetchBackfillEvents(agentId);
      m.redraw();

      if (isBackfillComplete(agentId)) {
        break;
      }

      const firstIdAfter = getFirstEventId(agentId);
      if (firstIdAfter === firstIdBefore) {
        stalledCount++;
        if (stalledCount >= MAX_STALLED_RETRIES) {
          break;
        }
        const delayMs = Math.min(BACKOFF_BASE_MS * 2 ** (stalledCount - 1), BACKOFF_CAP_MS);
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      } else {
        stalledCount = 0;
      }
    }
  }

  function startBackfill(agentId: string): void {
    if (backfillStarted || isBackfillComplete(agentId)) {
      return;
    }
    backfillStarted = true;
    runBackfillLoop(agentId);
  }

  function applyScrollPosition(element: HTMLElement): void {
    if (!userScrolledUp) {
      scrollToBottom(element);
      previousScrollTop = element.scrollTop;
    }
  }

  function handleScrollEvent(event: Event): void {
    const element = event.target as HTMLElement;
    const currentScrollTop = element.scrollTop;
    const didScrollUp = currentScrollTop < previousScrollTop;

    previousScrollTop = currentScrollTop;

    if (didScrollUp) {
      userScrolledUp = true;
      return;
    }

    if (isNearBottom(element)) {
      userScrolledUp = false;
    }
  }

  function renderMessages(agentId: string): m.Vnode {
    // If this agent is still being created, show the build log
    if (isProtoAgent(agentId)) {
      return renderBuildLog(agentId);
    }

    // Creation completed but failed -- keep the build log visible so the
    // user can read the error and the last few log lines. Without this the
    // build-log view transitions to the empty-chat / "no conversation data"
    // screen the instant proto_agent_completed arrives and the error flashes
    // by unreadably. The agent will never be added to getAgents() on
    // failure, so nothing else in the UI would surface the error either.
    if (logAgentId === agentId && logDone && !logSuccess) {
      return renderBuildLog(agentId);
    }

    // Agent finished creating successfully -- disconnect log WebSocket and
    // force reload
    if (logAgentId === agentId) {
      disconnectLogWs();
      currentAgentId = null;
    }

    ensureAgentLoaded(agentId);
    manageStreamConnection(agentId);

    if (isConversationNotFound(agentId)) {
      fetchScreenCapture(agentId);
      return m("div", { class: "message-list-not-found flex flex-col items-center justify-center h-full gap-4 p-8" }, [
        m("p", { class: "text-lg font-semibold text-text-primary" }, "No conversation data"),
        m("p", { class: "text-text-secondary" }, "This agent has no Claude session. It may have crashed on startup."),
        screenLoading
          ? m("p", { class: "text-text-secondary" }, "Loading terminal output...")
          : screenContent
            ? m(
                "pre",
                {
                  class:
                    "text-sm bg-gray-900 text-gray-100 p-4 rounded-lg overflow-auto w-full max-h-96 font-mono whitespace-pre",
                },
                screenContent,
              )
            : screenError
              ? m("p", { class: "text-text-secondary text-sm" }, `Could not capture terminal: ${screenError}`)
              : null,
      ]);
    }

    if (loading) {
      return m(
        "div",
        { class: "message-list-loading flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "Loading events..."),
      );
    }

    if (loadingError) {
      return m(
        "div",
        { class: "message-list-error flex items-center justify-center h-full" },
        m("p", { class: "text-red-500" }, `Error: ${loadingError}`),
      );
    }

    const events = getEventsForAgent(agentId);

    if (events.length === 0) {
      // No transcript yet -- but the user may have just sent their first
      // message, which should still show immediately as an optimistic bubble
      // rather than be hidden behind the empty-state placeholder.
      const pendingNodes = renderPendingMessages(agentId);
      if (pendingNodes.length === 0) {
        return m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "No events yet for this agent."),
        );
      }
      return m("div", { class: "message-list-wrapper" }, [m("div", { class: MESSAGE_LIST_CLASS }, pendingNodes)]);
    }

    startBackfill(agentId);

    const toolResults = buildToolResultsWithSkillExpansions(events);

    const hiddenEventIds = computeAuthErrorHiddenEventIds(events);
    const visibleEvents = events.filter((e) => !hiddenEventIds.has(e.event_id));

    // tk is an enrichment side-table (titles, summaries, pending roster),
    // joined onto the transcript-derived structure by id. It arrives as a
    // separate snapshot (GET /events + the step_enrichment SSE message), kept
    // current in the Response model; structure -- which steps exist, their
    // order, grouping -- comes purely from the transcript walk.
    const enrichment = getEnrichmentForAgent(agentId);
    const agent = getAgentById(agentId);
    const agentIsIdle = agent?.activity_state === "IDLE";

    // A single in-order walk of the transcript produces the turn sections:
    // each carries its timeline items (steps, ungrouped runs, chips) and its
    // wrap-up reply. There is no timestamp-based grouping or sorting.
    const sections = buildSections(visibleEvents, toolResults, enrichment, agentIsIdle);

    const messageNodes: m.Children[] = [];
    for (const section of sections) {
      if (section.user_event !== null) {
        const userNode = renderUserMessage(section.user_event);
        if (userNode !== null) messageNodes.push(userNode);
      }

      const hasSteps = section.items.some((i) => i.kind === "step");
      if (hasSteps) {
        messageNodes.push(
          m(ProgressBlock, {
            key: `progress-${section.key}`,
            items: section.items,
            trailing_reply: section.trailing_reply,
            toolResults,
            agentId,
          }),
        );
        continue;
      }

      // No steps this turn: render the body as plain chat -- prose and
      // tool-call blocks inline, the same as assistant messages outside a
      // progress section. Items are already in transcript order.
      for (const item of section.items) {
        if (item.kind === "ungrouped") {
          for (const e of item.events) messageNodes.push(renderAssistantMessage(e, toolResults, agentId));
        } else if (item.kind === "chip") {
          const chipNode = renderUserMessage(item.event);
          if (chipNode !== null) messageNodes.push(chipNode);
        }
      }
      for (const e of section.trailing_reply) messageNodes.push(renderAssistantMessage(e, toolResults, agentId));
    }

    return m("div", { class: "message-list-wrapper" }, [
      m("div", { class: MESSAGE_LIST_CLASS }, [...messageNodes, ...renderPendingMessages(agentId)]),
    ]);
  }

  return {
    onremove() {
      disconnectLogWs();
      if (currentAgentId !== null) {
        disconnectFromStream(currentAgentId);
      }
    },

    view(vnode) {
      const agentId = vnode.attrs.agentId;

      return m("div", { class: "chat-panel flex flex-col h-full relative" }, [
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            onscroll: handleScrollEvent,
            oncreate: (mainVnode: m.VnodeDOM) => {
              applyScrollPosition(mainVnode.dom as HTMLElement);
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              applyScrollPosition(mainVnode.dom as HTMLElement);
            },
          },
          isSlotClaimed("conversation-content") ? null : renderMessages(agentId),
        ),
        // Only show message input when not in proto-agent mode
        isProtoAgent(agentId)
          ? null
          : m("footer", { class: "app-footer" }, [
              m(EmptySlot, { name: "conversation-before-input" }),
              isConversationNotFound(agentId)
                ? null
                : m(ActivityIndicator, { agentId, events: getEventsForAgent(agentId) }),
              m(MessageInput, { agentId }),
              m("div", { class: "chat-agent-terminal-link" }, [
                m(
                  "button",
                  {
                    type: "button",
                    onclick: () => openAgentTerminalTab(agentId),
                  },
                  "Open agent terminal",
                ),
              ]),
            ]),
      ]);
    },
  };
}
