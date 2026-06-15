import m from "mithril";
import { apiUrl } from "../base-path";
import type {
  TranscriptEvent,
  AssistantMessageEvent,
  UserMessageEvent,
  ToolResultEvent,
  SubagentMetadata,
} from "../models/Response";
import { parseJsonMessage } from "../models/ws-json";
import { computeVisibleWindow } from "../models/virtualWindow";
import { nextUserScrolledUp } from "../models/scrollFollow";
import {
  createRowMeasurer,
  OVERSCAN_PX,
  ESTIMATED_USER_HEIGHT_PX,
  ESTIMATED_ASSISTANT_HEIGHT_PX,
} from "./row-measurement";
import { buildToolResultsWithSkillExpansions, renderAssistantMessageChildren } from "./message-renderers";

interface SubagentViewAttrs {
  agentId: string;
  subagentSessionId: string;
}

interface SubagentEventsResponse {
  events: TranscriptEvent[];
  metadata: SubagentMetadata | null;
}

interface RowDescriptor {
  key: string;
  estimate: number;
  render: () => m.Vnode;
}

function renderUserMessage(event: UserMessageEvent): m.Vnode {
  return m("div", { id: event.event_id, class: "message message-user", key: event.event_id }, [
    m("div", { class: "message-user-bubble" }, [
      m("div", { class: "message-content whitespace-pre-wrap" }, event.content || ""),
    ]),
  ]);
}

function renderAssistantMessage(
  event: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  agentId: string,
): m.Vnode {
  return m(
    "div",
    { id: event.event_id, class: "message message-assistant", key: event.event_id },
    m("div", renderAssistantMessageChildren(event, toolResults, agentId)),
  );
}

function buildRows(agentId: string, events: TranscriptEvent[]): RowDescriptor[] {
  // Skill-expansion user_messages are folded into their Skill tool call's output
  // (same as the main panel) rather than rendered as separate rows.
  const toolResults = buildToolResultsWithSkillExpansions(events);

  const rows: RowDescriptor[] = [];
  for (const event of events) {
    if (event.type === "user_message") {
      rows.push({
        key: event.event_id,
        estimate: ESTIMATED_USER_HEIGHT_PX,
        render: () => renderUserMessage(event),
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

export function SubagentView(): m.Component<SubagentViewAttrs> {
  let events: TranscriptEvent[] = [];
  // Persistent dedup set so each live SSE delta is O(1), not an O(n) rebuild.
  const eventIds = new Set<string>();
  let metadata: SubagentMetadata | null = null;
  let loading = true;
  let loadingError: string | null = null;
  let eventSource: EventSource | null = null;

  // Virtualization state (a subagent transcript is bounded but can still be
  // large; only the viewport window is rendered to the DOM).
  let scrollEl: HTMLElement | null = null;
  let viewportHeight = 0;
  let scrollTop = 0;
  const rowMeasurer = createRowMeasurer();
  let userScrolledUp = false;
  let previousScrollTop = 0;
  let viewportResizeObserver: ResizeObserver | null = null;
  // Memoized rows. buildRows walks the whole subagent transcript, so it is
  // recomputed only when the event set changes -- not on every scroll redraw.
  // The transcript is append-only here (no in-place upgrades, no eviction), so
  // the event count is a sufficient cache key.
  let rowsCacheKey = "";
  let cachedRows: RowDescriptor[] = [];

  function addEvents(incoming: TranscriptEvent[]): boolean {
    let added = false;
    for (const event of incoming) {
      if (!eventIds.has(event.event_id)) {
        eventIds.add(event.event_id);
        events.push(event);
        added = true;
      }
    }
    return added;
  }

  async function fetchSubagentEvents(agentId: string, subagentSessionId: string): Promise<void> {
    loading = true;
    loadingError = null;

    try {
      const result = await m.request<SubagentEventsResponse>({
        method: "GET",
        url: apiUrl(
          `/api/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentSessionId)}/events`,
        ),
      });
      events = [];
      eventIds.clear();
      addEvents(result.events);
      metadata = result.metadata ?? null;
      loading = false;
    } catch (error) {
      loading = false;
      loadingError = (error as Error).message ?? String(error);
    }
  }

  function connectToStream(agentId: string, subagentSessionId: string): void {
    if (eventSource !== null) {
      return;
    }

    const url = apiUrl(
      `/api/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentSessionId)}/stream`,
    );
    eventSource = new EventSource(url);

    eventSource.onmessage = (messageEvent: MessageEvent) => {
      const event = parseJsonMessage<TranscriptEvent>(messageEvent.data);
      if (event === null) {
        return;
      }
      if (addEvents([event])) {
        m.redraw();
      }
    };

    eventSource.onerror = () => {
      if (eventSource !== null) {
        eventSource.close();
        eventSource = null;
      }
    };
  }

  function disconnectFromStream(): void {
    if (eventSource !== null) {
      eventSource.close();
      eventSource = null;
    }
  }

  function applyScrollPosition(element: HTMLElement): void {
    if (!userScrolledUp) {
      element.scrollTop = element.scrollHeight;
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
    // A subagent transcript is a single loaded list with no off-tail jump, so
    // there is never newer unloaded history below: hasMoreAfter is always false.
    userScrolledUp = nextUserScrolledUp({
      didScrollUp,
      isNearBottom: element.scrollHeight - element.scrollTop - element.clientHeight < 40,
      hasMoreAfter: false,
    });
  }

  // Refresh the cached viewport height and schedule a measure pass; the
  // measure/cache mechanics live in the shared row measurer.
  function scheduleMeasure(): void {
    if (scrollEl !== null) {
      viewportHeight = scrollEl.clientHeight;
    }
    rowMeasurer.scheduleMeasure(() => scrollEl);
  }

  function renderWindowedList(agentId: string): m.Vnode {
    const renderKey = `${agentId}|${events.length}`;
    if (renderKey !== rowsCacheKey) {
      cachedRows = buildRows(agentId, events);
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;
    const getHeight = (index: number): number => rowMeasurer.getHeight(rows[index].key) ?? rows[index].estimate;
    const windowResult = computeVisibleWindow({
      count: rows.length,
      getHeight,
      scrollTop,
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
    oninit(vnode) {
      const { agentId, subagentSessionId } = vnode.attrs;
      fetchSubagentEvents(agentId, subagentSessionId).then(() => {
        connectToStream(agentId, subagentSessionId);
      });
    },

    onremove() {
      disconnectFromStream();
      if (viewportResizeObserver !== null) {
        viewportResizeObserver.disconnect();
        viewportResizeObserver = null;
      }
      scrollEl = null;
    },

    view(vnode) {
      const { agentId } = vnode.attrs;
      const title = metadata?.description || "Sub-agent conversation";
      const agentType = metadata?.agent_type || "";

      const header = m("header", { class: "app-header" }, [
        m("h1", { class: "app-header-title" }, title),
        agentType ? m("span", { class: "app-header-model-badge" }, agentType) : null,
      ]);

      let content: m.Vnode;

      if (loading) {
        content = m(
          "div",
          { class: "message-list-loading flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "Loading events..."),
        );
      } else if (loadingError) {
        content = m(
          "div",
          { class: "message-list-error flex items-center justify-center h-full" },
          m("p", { class: "text-red-500" }, `Error: ${loadingError}`),
        );
      } else if (events.length === 0) {
        content = m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "No events yet."),
        );
      } else {
        content = renderWindowedList(agentId);
      }

      return m("div", { class: "app-content-wrapper flex-1 flex flex-col min-h-0" }, [
        header,
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            onscroll: handleScrollEvent,
            oncreate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              viewportHeight = scrollEl.clientHeight;
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
          content,
        ),
        // No footer/message input -- read-only
      ]);
    },
  };
}
