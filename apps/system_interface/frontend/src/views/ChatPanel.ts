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
  fetchForwardEvents,
  fetchWindowAtOffset,
  getEventsForAgent,
  getEventCount,
  getFirstOffset,
  getRenderVersion,
  getTotalEventCount,
  evictOldEvents,
  hasMoreBefore,
  hasMoreAfter,
  isConversationNotFound,
  MAX_HELD_EVENTS,
} from "../models/Response";
import { computeVisibleWindow } from "../models/virtualWindow";
import { nextUserScrolledUp } from "../models/scrollFollow";
import { createRowMeasurer, OVERSCAN_PX } from "./row-measurement";
import { connectToStream, disconnectFromStream, loadSnapshotWithStream } from "../models/StreamingMessage";
import { getAgentById, getProtoAgents } from "../models/AgentManager";
import { openLoginModal } from "../models/ClaudeAuth";
import { apiUrl } from "../base-path";
import { EmptySlot } from "./EmptySlot";
import { MessageInput } from "./MessageInput";
import { buildAgentTerminalUrl, getTerminalUrl, openIframeTabForAgent } from "./DockviewWorkspace";
import { buildConversationRows, type RowDescriptor } from "./conversation-rows";
import { ActivityIndicator } from "./ActivityIndicator";
import { renderPendingMessages } from "./PendingMessageView";

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
// Backfill fires when the viewport is within this many pixels of the top or
// bottom edge of the loaded rows (and the server reports more history there).
const BACKFILL_TRIGGER_PX = 600;
// When the scroll position maps to an event more than this many events beyond the
// loaded window, jump (replace the window around the target) instead of paging
// there incrementally. Small enough that ordinary scrolling keeps paging; large
// enough that a couple of pages' overshoot doesn't trigger a disruptive reload.
const JUMP_GAP_EVENTS = 120;
// Stable per-event height used to size the reserved (phantom) regions for history
// that exists on the server but isn't loaded yet. It is deliberately a constant
// rather than the measured average of the loaded window: the loaded window is a
// tiny fraction of a long transcript (e.g. 50 of 5000+ events), so its measured
// average -- which shifts every frame as rows measure -- would be amplified by the
// large unloaded count into wild scrollbar jumps. A constant keeps the total
// scroll height (~ total * this) stable, so the scrollbar thumb doesn't churn and
// an offset jump lands at a fixed position. Its exact value isn't UX-critical:
// the drag fraction -> event index mapping and the post-jump thumb position both
// scale with it and so are independent of it; only the loaded window's small
// residual (measured height vs count * this) is affected.
const ESTIMATED_EVENT_HEIGHT_PX = 160;

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollToBottom(element: HTMLElement): void {
  element.scrollTop = element.scrollHeight;
}

function isProtoAgent(agentId: string): boolean {
  return getProtoAgents().some((p) => p.agent_id === agentId);
}

export function ChatPanel(): m.Component<{ agentId: string }> {
  let loading = false;
  let loadingError: string | null = null;
  let currentAgentId: string | null = null;
  let userScrolledUp = false;

  // Virtualization state.
  let scrollEl: HTMLElement | null = null;
  let viewportHeight = 0;
  let scrollTop = 0;
  // Previous observed scroll position, for detecting scroll direction. Updated in
  // lockstep with scrollTop at every programmatic scroll site (see handleScrollEvent).
  let previousScrollTop = 0;
  const rowMeasurer = createRowMeasurer();
  let viewportResizeObserver: ResizeObserver | null = null;
  // Memoized turn-grouping output. buildSections walks the whole held
  // transcript, so it is recomputed only when the data actually changes (keyed
  // on the render version + idle flag), not on every scroll-driven redraw.
  let rowsCacheKey: string | null = null;
  let cachedRows: RowDescriptor[] = [];
  // Heights reserved above/below the loaded window for history that exists on the
  // server but isn't loaded yet (see renderMessages). Shared so the scroll handler
  // can tell when the viewport is over a reserved region and page/jump/overlay
  // accordingly.
  let phantomTopHeight = 0;
  let phantomBottomHeight = 0;
  // Paging (scroll-driven fetch) in-flight guard. Covers older/newer pages and
  // offset jumps -- only one is outstanding at a time.
  let backfillInFlight = false;
  // After an offset jump replaces the window, pin the viewport once to the top of
  // the freshly loaded rows (just below the top reserved spacer) so the user lands
  // on the jumped-to content rather than in the reserved region above it. With the
  // reserved heights now sized by a stable constant, the top of the loaded window
  // doesn't drift as rows measure, so a single pin suffices -- no timed settle.
  let pendingPinToWindowTop = false;
  // After a backfill prepend, compensate scrollTop by the height the content
  // grew so the user's viewport stays anchored instead of jumping. The pending
  // flag is only raised once the backfill resolves, so unrelated redraws in the
  // meantime do not consume (and discard) the captured pre-prepend height.
  let scrollHeightBeforePrepend = 0;
  let prependCompensationPending = false;

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
    scrollTop = 0;
    previousScrollTop = 0;
    userScrolledUp = false;
    backfillInFlight = false;
    scrollHeightBeforePrepend = 0;
    prependCompensationPending = false;
    rowMeasurer.reset();
    loadAgent(agentId);
  }

  /**
   * Keep the loaded window in step with the scroll position. Three cases, all
   * bounded to a single fetch:
   *   - viewport far from the loaded window (e.g. a scrollbar drag deep into
   *     history): JUMP -- replace the window with a page around the target offset,
   *     so reaching a distant point costs one request, not a walk through
   *     everything between.
   *   - viewport near the top edge of the loaded rows, with older history left:
   *     page one older window-worth.
   *   - viewport near the bottom edge, with newer history left (only possible
   *     after a jump moved the window off the live tail): page one newer worth.
   */
  function maybePage(agentId: string, element: HTMLElement): void {
    // A fetch is already outstanding (only one at a time), or a just-completed jump
    // still needs its one-shot pin applied -- in both cases the window is about to
    // change, so don't act on the current (transient) scroll position.
    if (backfillInFlight || pendingPinToWindowTop) {
      return;
    }
    const total = getTotalEventCount(agentId);
    const held = getEventCount(agentId);
    const firstOffset = getFirstOffset(agentId);
    const windowEnd = firstOffset + held;

    // The event index roughly under the viewport top, from the scroll fraction.
    const fraction = element.scrollHeight > 0 ? element.scrollTop / element.scrollHeight : 0;
    const targetIndex = Math.round(fraction * total);

    // Far from the loaded window in either direction -> jump.
    if (targetIndex < firstOffset - JUMP_GAP_EVENTS || targetIndex > windowEnd + JUMP_GAP_EVENTS) {
      backfillInFlight = true;
      fetchWindowAtOffset(agentId, targetIndex - Math.floor(JUMP_GAP_EVENTS / 2)).finally(() => {
        backfillInFlight = false;
        // The window now sits off the live tail, so stop following it, and pin the
        // viewport once to the new window's top on the next redraw (applyScrollPosition).
        userScrolledUp = true;
        pendingPinToWindowTop = true;
        m.redraw();
      });
      return;
    }

    // Near the top of the loaded rows -> page older (anchored so the view doesn't
    // jump when the older page lands above).
    if (hasMoreBefore(agentId) && element.scrollTop - phantomTopHeight < BACKFILL_TRIGGER_PX) {
      backfillInFlight = true;
      scrollHeightBeforePrepend = element.scrollHeight;
      fetchBackfillEvents(agentId).finally(() => {
        backfillInFlight = false;
        prependCompensationPending = true;
        m.redraw();
      });
      return;
    }

    // Near the bottom of the loaded rows with newer history left -> page newer.
    // Appending below shifts nothing above it, so no scroll compensation is due.
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    if (hasMoreAfter(agentId) && distanceFromBottom - phantomBottomHeight < BACKFILL_TRIGGER_PX) {
      backfillInFlight = true;
      fetchForwardEvents(agentId).finally(() => {
        backfillInFlight = false;
        m.redraw();
      });
    }
  }

  function applyScrollPosition(element: HTMLElement): void {
    // After an offset jump, pin the viewport once to the top of the freshly loaded
    // rows (just below the top reserved spacer) so the user lands on the jumped-to
    // content rather than in the reserved (blank) region above it. The reserved
    // top height is a stable constant * offset, so it doesn't drift as the loaded
    // rows measure -- a single pin lands correctly without a timed settle.
    if (pendingPinToWindowTop) {
      pendingPinToWindowTop = false;
      element.scrollTop = phantomTopHeight;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
      return;
    }

    // Compensate for the change in document height above the viewport caused by a
    // just-completed older-page load, so the viewport stays anchored on what the
    // user was reading rather than the thumb jumping. Done before the
    // scroll-to-bottom check below; the two are mutually exclusive in practice (a
    // prepend only happens while scrolled up).
    //
    // The delta is signed and BOTH signs must be applied. The top phantom shrinks
    // by exactly (page events * ESTIMATED_EVENT_HEIGHT_PX), but the events render
    // as grouped rows (a whole turn can be one row) whose real height rarely equals
    // that, so the net document height above the viewport usually *shrinks*
    // (delta < 0). Only compensating positive deltas left those shrinks
    // uncompensated: scrollTop stayed put while scrollHeight dropped, nudging the
    // thumb down a few px on every page -- the "scroll up, jump down, scroll up,
    // jump down" seen on a fast mouse-wheel scroll. Clamp to phantomTopHeight so a
    // negative delta keeps the viewport within the freshly loaded content (we just
    // loaded older events, so it belongs there) rather than slipping back into the
    // reserved region above.
    if (prependCompensationPending) {
      prependCompensationPending = false;
      const delta = element.scrollHeight - scrollHeightBeforePrepend;
      if (delta !== 0) {
        element.scrollTop = Math.max(phantomTopHeight, element.scrollTop + delta);
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
    // applyScrollPosition keeps previousScrollTop in lockstep with its own
    // programmatic re-pins, so only a genuine user scroll registers as movement.
    const didScrollUp = element.scrollTop < previousScrollTop;
    previousScrollTop = element.scrollTop;
    scrollTop = element.scrollTop;

    userScrolledUp = nextUserScrolledUp({
      didScrollUp,
      isNearBottom: isNearBottom(element),
      hasMoreAfter: hasMoreAfter(currentAgentId ?? ""),
    });

    if (currentAgentId !== null) {
      maybePage(currentAgentId, element);
    }
  }

  // Refresh the cached viewport height and schedule a measure pass. Kept local
  // so the viewport height (used by the window math below) stays current; the
  // measure/cache mechanics themselves live in the shared row measurer.
  function scheduleMeasure(): void {
    if (scrollEl !== null) {
      viewportHeight = scrollEl.clientHeight;
    }
    rowMeasurer.scheduleMeasure(() => scrollEl);
  }

  function renderMessages(agentId: string): m.Vnode {
    // Reset here so the loading overlay (keyed on a positive value) stays hidden
    // for every path that doesn't render the windowed list; the windowed path
    // below sets the real reserved heights.
    phantomTopHeight = 0;
    phantomBottomHeight = 0;

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
    // history is re-fetched via backfill on scroll-up (evictOldEvents advances the
    // window start so it reads as older history above). Re-pinned to the bottom by
    // applyScrollPosition afterwards.
    if (!userScrolledUp && getEventCount(agentId) > MAX_HELD_EVENTS) {
      evictOldEvents(agentId);
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

    const agent = getAgentById(agentId);
    const agentIsIdle = agent?.activity_state === "IDLE";

    // Memoize the turn-grouping -> rows pipeline. buildSections walks the entire
    // held transcript, so recomputing it on every scroll-driven redraw is the
    // dominant scroll cost on a long conversation. Its output depends only on the
    // held events and the idle flag -- captured by the render version (bumped on
    // any data mutation) plus the idle flag -- so a scroll-only redraw reuses the
    // cached rows. The grouping (steps, decoration, skill expansions, auth-error
    // hiding) is produced by the same functions on the same inputs, so the
    // rendered structure is identical to recomputing.
    const renderKey = `${agentId}|${getRenderVersion(agentId)}|${agentIsIdle ? 1 : 0}`;
    if (renderKey !== rowsCacheKey) {
      // Both structure and decoration come from the transcript walk; there is no
      // side-channel enrichment. The same pipeline feeds the subagent view, so a
      // subagent's "View conversation" renders an identical progress timeline.
      cachedRows = buildConversationRows(agentId, events, agentIsIdle);
      rowMeasurer.prune(new Set(cachedRows.map((row) => row.key)));
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;

    const getHeight = (index: number): number => rowMeasurer.getHeight(rows[index].key) ?? rows[index].estimate;

    // Reserve space above and below the loaded window for history that exists on
    // the server but isn't loaded yet, so the scrollbar reflects the whole
    // conversation rather than just the loaded window -- and so paging more in
    // doesn't make it jump. Each reserve is the count of not-yet-loaded events on
    // that side times a stable per-event constant (see ESTIMATED_EVENT_HEIGHT_PX).
    // Using a constant (not the loaded window's measured average) is what keeps the
    // total scroll height stable: deriving it from the small loaded window would
    // make every row measurement, amplified by the large unloaded count, jolt the
    // scrollbar. As events page in, the reserve shrinks by ~the height they add, so
    // existing content stays put.
    const total = getTotalEventCount(agentId);
    const firstOffset = getFirstOffset(agentId);
    const olderUnloaded = Math.max(0, firstOffset);
    const newerUnloaded = Math.max(0, total - (firstOffset + events.length));
    phantomTopHeight = Math.round(olderUnloaded * ESTIMATED_EVENT_HEIGHT_PX);
    phantomBottomHeight = Math.round(newerUnloaded * ESTIMATED_EVENT_HEIGHT_PX);

    const windowResult = computeVisibleWindow({
      count: rows.length,
      getHeight,
      // The loaded rows start below the top phantom spacer, so shift the scroll
      // position into the loaded rows' own coordinate space for the window math.
      scrollTop: Math.max(0, scrollTop - phantomTopHeight),
      // Before the first measure viewportHeight is 0; fall back to the live
      // clientHeight (or a large value) so the initial render is not a 1-row
      // sliver that the post-mount measure then has to expand.
      viewportHeight: viewportHeight > 0 ? viewportHeight : (scrollEl?.clientHeight ?? 2000),
      overscanPx: OVERSCAN_PX,
    });

    const visibleRows: m.Children[] = [];
    visibleRows.push(m("div", { key: "__spacer_top", style: `height: ${phantomTopHeight + windowResult.topPad}px` }));
    for (let i = windowResult.startIndex; i < windowResult.endIndex; i++) {
      visibleRows.push(rows[i].render());
    }
    visibleRows.push(
      m("div", { key: "__spacer_bottom", style: `height: ${windowResult.bottomPad + phantomBottomHeight}px` }),
    );

    return m("div", { class: "message-list-wrapper" }, [
      // Pending (optimistic) messages render after the virtualized rows so a
      // just-sent bubble shows at the live tail until its real event lands.
      m("div", { class: MESSAGE_LIST_CLASS }, [...visibleRows, ...renderPendingMessages(agentId)]),
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

      // renderMessages sets the reserved heights, so build the content first, then
      // decide whether the viewport currently sits over a reserved region (above
      // all loaded rows, or below them) and so should show a loading overlay
      // instead of a blank spacer while the fetch for that region lands.
      const content = isSlotClaimed("conversation-content") ? null : renderMessages(agentId);
      const viewportPx = viewportHeight > 0 ? viewportHeight : (scrollEl?.clientHeight ?? 0);
      const loadedTop = phantomTopHeight;
      const loadedBottom = scrollEl !== null ? scrollEl.scrollHeight - phantomBottomHeight : Number.MAX_SAFE_INTEGER;
      const inReservedRegion =
        (phantomTopHeight > 0 && scrollTop < loadedTop) ||
        (phantomBottomHeight > 0 && scrollTop + viewportPx > loadedBottom);

      return m("div", { class: "chat-panel flex flex-col h-full relative" }, [
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
              if (currentAgentId !== null) {
                maybePage(currentAgentId, scrollEl);
              }
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              applyScrollPosition(scrollEl);
              scheduleMeasure();
              // Drive paging from the render loop, not only from scroll events, so
              // the viewport sitting over a reserved region always triggers (or
              // already has in flight) the fetch to cover it. Without this a drag
              // that ends in a reserved region -- with the triggering scroll event
              // suppressed by an in-flight fetch -- could strand the loading overlay
              // with nothing actually loading.
              if (currentAgentId !== null) {
                maybePage(currentAgentId, scrollEl);
              }
            },
          },
          content,
        ),
        // While the viewport is over reserved space for not-yet-loaded history
        // (e.g. the scrollbar was dragged into a region the loaded window doesn't
        // cover yet), overlay a loading indicator centered in the viewport so the
        // user never sees a blank area. pointer-events:none so it never blocks scroll.
        inReservedRegion
          ? m(
              "div",
              {
                class:
                  "message-list-window-loading absolute inset-0 flex items-center justify-center p-6 pointer-events-none",
              },
              m("p", { class: "text-text-secondary" }, "Loading messages..."),
            )
          : null,
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
