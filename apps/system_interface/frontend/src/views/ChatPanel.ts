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
  getEventCount,
  evictOldEvents,
  hasMoreToBackfill,
  isConversationNotFound,
  MAX_HELD_EVENTS,
  type TranscriptEvent,
} from "../models/Response";
import { computeVisibleWindow } from "../models/virtualWindow";
import { connectToStream, disconnectFromStream, loadSnapshotWithStream } from "../models/StreamingMessage";
import { getAgentById, getProtoAgents } from "../models/AgentManager";
import { apiUrl } from "../base-path";
import { EmptySlot } from "./EmptySlot";
import { MessageInput } from "./MessageInput";
import { renderUserMessage, renderAssistantMessage, isHiddenUserMessage } from "./message-renderers";
import { getTerminalUrl, openIframeTabForAgent } from "./DockviewWorkspace";

function getAgentTerminalUrl(agentId: string): string {
  const baseUrl = getTerminalUrl();
  const separator = baseUrl.includes("?") ? "&" : "?";
  // The ttyd dispatch script is invoked as `bash -c "$SCRIPT" <args...>` where
  // the first trailing arg becomes $0 (not $1). The dispatch reads KEY="$1",
  // so we prepend a dummy "_" to land the real key in $1. That matches the
  // pattern used by the existing workdir deep-link in DockviewWorkspace.ts.
  // Passing the agent name as $2 lets agent.sh attach to that agent's tmux
  // session ("${MNGR_PREFIX}<name>") rather than the primary agent's. If the
  // agent isn't in the local cache yet, fall back to no name arg and let
  // agent.sh attach to the ambient session.
  const agent = getAgentById(agentId);
  const args = agent?.name ? `arg=_&arg=agent&arg=${encodeURIComponent(agent.name)}` : "arg=_&arg=agent";
  return `${baseUrl}${separator}${args}`;
}

function openAgentTerminalTab(agentId: string): void {
  const agent = getAgentById(agentId);
  const title = agent?.name ? `${agent.name} terminal` : "agent terminal";
  openIframeTabForAgent(agentId, getAgentTerminalUrl(agentId), title);
}

const SCROLL_BOTTOM_THRESHOLD_PX = 40;

// Pixels rendered above/below the viewport so scrolling does not flash blank
// before the next redraw fills the window.
const OVERSCAN_PX = 800;
// Scroll-up backfill fires when the viewport top is within this many pixels of
// the top of the held content (and the server reports more history).
const BACKFILL_TRIGGER_PX = 600;
// Per-type fallback row heights, used until a row has been measured. Rough is
// fine: they only affect spacer sizing for off-screen rows, which is corrected
// as rows scroll into view and are measured.
const ESTIMATED_USER_HEIGHT_PX = 90;
const ESTIMATED_ASSISTANT_HEIGHT_PX = 240;

interface RowDescriptor {
  key: string;
  estimate: number;
  render: () => m.Vnode;
}

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollToBottom(element: HTMLElement): void {
  element.scrollTop = element.scrollHeight;
}

function isProtoAgent(agentId: string): boolean {
  return getProtoAgents().some((p) => p.agent_id === agentId);
}

/**
 * Build the renderable row list (user + assistant messages) from the held
 * events. tool_result events are not rows -- they are folded into the assistant
 * message that issued the call -- and hidden user messages are dropped. Render
 * closures are invoked lazily so only windowed rows actually build their vnodes.
 */
function buildRows(agentId: string, events: TranscriptEvent[]): RowDescriptor[] {
  const toolResults = new Map<string, TranscriptEvent>();
  for (const event of events) {
    if (event.type === "tool_result" && event.tool_call_id) {
      toolResults.set(event.tool_call_id, event);
    }
  }

  const rows: RowDescriptor[] = [];
  for (const event of events) {
    if (event.type === "user_message") {
      if (isHiddenUserMessage(event.content || "")) {
        continue;
      }
      rows.push({
        key: event.event_id,
        estimate: ESTIMATED_USER_HEIGHT_PX,
        render: () => renderUserMessage(event) as m.Vnode,
      });
    } else if (event.type === "assistant_message") {
      rows.push({
        key: event.event_id,
        estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
        render: () => renderAssistantMessage(event, toolResults, agentId),
      });
    }
  }
  return rows;
}

export function ChatPanel(): m.Component<{ agentId: string }> {
  let loading = false;
  let loadingError: string | null = null;
  let currentAgentId: string | null = null;
  let userScrolledUp = false;
  let previousScrollTop = 0;

  // Virtualization state.
  let scrollEl: HTMLElement | null = null;
  let viewportHeight = 0;
  let scrollTop = 0;
  let rowHeights = new Map<string, number>();
  let viewportResizeObserver: ResizeObserver | null = null;
  let measureScheduled = false;
  // Backfill (scroll-up paging) state.
  let backfillInFlight = false;
  // After a backfill prepend, compensate scrollTop by the height the content
  // grew so the user's viewport stays anchored instead of jumping. The pending
  // flag is only raised once the backfill resolves, so unrelated redraws in the
  // meantime do not consume (and discard) the captured pre-prepend height.
  let scrollHeightBeforePrepend = 0;
  let prependCompensationPending = false;

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
    scrollTop = 0;
    userScrolledUp = false;
    backfillInFlight = false;
    scrollHeightBeforePrepend = 0;
    prependCompensationPending = false;
    rowHeights = new Map<string, number>();
    loadAgent(agentId);
  }

  /**
   * Fetch one older page when the user scrolls near the top and the server has
   * more history. Replaces the old drain-to-completion loop: history is paged in
   * on demand, one viewport-worth at a time, so opening a long transcript no
   * longer pulls the entire backlog to the client.
   */
  function maybeBackfill(agentId: string, element: HTMLElement): void {
    if (backfillInFlight || !hasMoreToBackfill(agentId)) {
      return;
    }
    if (element.scrollTop > BACKFILL_TRIGGER_PX) {
      return;
    }
    backfillInFlight = true;
    scrollHeightBeforePrepend = element.scrollHeight;
    fetchBackfillEvents(agentId).finally(() => {
      backfillInFlight = false;
      // Only now (older events prepended) is compensation due; raising the flag
      // here keeps interim redraws from consuming the captured height early.
      prependCompensationPending = true;
      m.redraw();
    });
  }

  function applyScrollPosition(element: HTMLElement): void {
    // Compensate for content prepended by a just-completed backfill so the
    // viewport stays anchored on what the user was reading rather than jumping
    // to the new top. Done before the scroll-to-bottom check below; the two are
    // mutually exclusive in practice (a prepend only happens while scrolled up).
    if (prependCompensationPending) {
      prependCompensationPending = false;
      const delta = element.scrollHeight - scrollHeightBeforePrepend;
      if (delta > 0) {
        element.scrollTop += delta;
        scrollTop = element.scrollTop;
        previousScrollTop = element.scrollTop;
      }
    }

    if (!userScrolledUp) {
      scrollToBottom(element);
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
    }
  }

  function handleScrollEvent(event: Event): void {
    const element = event.target as HTMLElement;
    const currentScrollTop = element.scrollTop;
    const didScrollUp = currentScrollTop < previousScrollTop;

    previousScrollTop = currentScrollTop;
    scrollTop = currentScrollTop;

    if (didScrollUp) {
      userScrolledUp = true;
      if (currentAgentId !== null) {
        maybeBackfill(currentAgentId, element);
      }
      return;
    }

    if (isNearBottom(element)) {
      userScrolledUp = false;
    }
  }

  // Read each rendered row's height from the DOM and cache it by event id, so
  // the window math and spacer sizes converge on real heights. Returns whether
  // any height changed (so the caller can schedule one more redraw to settle
  // the spacers). Also refreshes the viewport height.
  function measureRows(): boolean {
    if (scrollEl === null) {
      return false;
    }
    viewportHeight = scrollEl.clientHeight;
    const list = scrollEl.querySelector(".message-list");
    if (list === null) {
      return false;
    }
    let changed = false;
    for (const child of Array.from(list.children)) {
      const element = child as HTMLElement;
      const key = element.id;
      if (key === "") {
        continue; // spacer
      }
      const height = element.offsetHeight;
      if (height > 0 && rowHeights.get(key) !== height) {
        rowHeights.set(key, height);
        changed = true;
      }
    }
    return changed;
  }

  function scheduleMeasure(): void {
    if (measureScheduled) {
      return;
    }
    measureScheduled = true;
    requestAnimationFrame(() => {
      measureScheduled = false;
      if (measureRows()) {
        m.redraw();
      }
    });
  }

  // Keep the height cache from growing without bound as rows are evicted: drop
  // entries for keys no longer present once it drifts well past the row count.
  function pruneHeights(keys: Set<string>): void {
    if (rowHeights.size <= keys.size + 256) {
      return;
    }
    for (const key of rowHeights.keys()) {
      if (!keys.has(key)) {
        rowHeights.delete(key);
      }
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

    // Bound client memory while following the live tail: trim the oldest held
    // events once well over the cap. Only when at the bottom, so a scrolled-up
    // reader's rendered history is never yanked out from under them; the dropped
    // history is re-fetched via backfill on scroll-up (evictOldEvents sets
    // has_more). Re-pinned to the bottom by applyScrollPosition afterwards.
    if (!userScrolledUp && getEventCount(agentId) > MAX_HELD_EVENTS) {
      evictOldEvents(agentId);
    }

    const events = getEventsForAgent(agentId);

    if (events.length === 0) {
      return m(
        "div",
        { class: "message-list-empty flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "No events yet for this agent."),
      );
    }

    const rows = buildRows(agentId, events);
    pruneHeights(new Set(rows.map((row) => row.key)));

    const getHeight = (index: number): number => rowHeights.get(rows[index].key) ?? rows[index].estimate;
    const windowResult = computeVisibleWindow({
      count: rows.length,
      getHeight,
      scrollTop,
      // Before the first measure viewportHeight is 0; fall back to the live
      // clientHeight (or a large value) so the initial render is not a 1-row
      // sliver that the post-mount measure then has to expand.
      viewportHeight: viewportHeight > 0 ? viewportHeight : (scrollEl?.clientHeight ?? 2000),
      overscanPx: OVERSCAN_PX,
    });

    const visibleRows: m.Vnode[] = [];
    visibleRows.push(m("div", { key: "__spacer_top", style: `height: ${windowResult.topPad}px` }));
    for (let i = windowResult.startIndex; i < windowResult.endIndex; i++) {
      visibleRows.push(rows[i].render());
    }
    visibleRows.push(m("div", { key: "__spacer_bottom", style: `height: ${windowResult.bottomPad}px` }));

    return m("div", { class: "message-list-wrapper" }, [
      m(
        "div",
        { class: "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6" },
        visibleRows,
      ),
    ]);
  }

  return {
    onremove() {
      disconnectLogWs();
      if (viewportResizeObserver !== null) {
        viewportResizeObserver.disconnect();
        viewportResizeObserver = null;
      }
      scrollEl = null;
      if (currentAgentId !== null) {
        disconnectFromStream(currentAgentId);
      }
    },

    view(vnode) {
      const agentId = vnode.attrs.agentId;

      return m("div", { class: "chat-panel flex flex-col h-full" }, [
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            onscroll: handleScrollEvent,
            oncreate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              viewportHeight = scrollEl.clientHeight;
              // Recompute the window when the panel itself resizes (dockview
              // splits, window resize) since that changes the visible range.
              viewportResizeObserver = new ResizeObserver(() => {
                if (scrollEl !== null && scrollEl.clientHeight !== viewportHeight) {
                  viewportHeight = scrollEl.clientHeight;
                  m.redraw();
                }
              });
              viewportResizeObserver.observe(scrollEl);
              applyScrollPosition(scrollEl);
              scheduleMeasure();
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              applyScrollPosition(scrollEl);
              scheduleMeasure();
            },
          },
          isSlotClaimed("conversation-content") ? null : renderMessages(agentId),
        ),
        // Only show message input when not in proto-agent mode
        isProtoAgent(agentId)
          ? null
          : m("footer", { class: "app-footer" }, [
              m(EmptySlot, { name: "conversation-before-input" }),
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
