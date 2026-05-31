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
import type { TranscriptEvent } from "../models/Response";
import {
  fetchEvents,
  fetchBackfillEvents,
  getEventsForAgent,
  getFirstEventId,
  isConversationNotFound,
  isBackfillComplete,
} from "../models/Response";
import { connectToStream, disconnectFromStream } from "../models/StreamingMessage";
import { getAgentById, getProtoAgents } from "../models/AgentManager";
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
import { getTerminalUrl, openIframeTabForAgent } from "./DockviewWorkspace";
import {
  buildTaskRecords,
  buildSectionSteps,
  attributeNarration,
  classifyTopLevelMessages,
  placeStopHookChips,
} from "./turn-grouping";
import { isNonBoundaryUserMessage, isStopHookFeedback } from "./user-message-classification";
import { ProgressBlock } from "./ProgressBlock";
import { ActivityIndicator } from "./ActivityIndicator";

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
      await fetchEvents(agentId);
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
      return m(
        "div",
        { class: "message-list-empty flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "No events yet for this agent."),
      );
    }

    startBackfill(agentId);

    const toolResults = buildToolResultsWithSkillExpansions(events);

    const hiddenEventIds = computeAuthErrorHiddenEventIds(events);
    const visibleEvents = events.filter((e) => !hiddenEventIds.has(e.event_id));

    const taskRecords = buildTaskRecords(visibleEvents);
    const agent = getAgentById(agentId);
    const agentIsIdle = agent?.activity_state === "IDLE";

    const messageNodes: m.Children[] = [];
    let sectionUserEvent: TranscriptEvent | null = null;
    let sectionStart = "";
    let bodyEvents: TranscriptEvent[] = [];

    function flushSection(nextBoundaryTs: string): void {
      if (sectionUserEvent === null) return;
      const endTs = nextBoundaryTs;
      const isTail = endTs === "";
      const isSettled = !isTail || agentIsIdle;

      const steps = buildSectionSteps(taskRecords, sectionStart, endTs, isSettled);
      attributeNarration(steps, bodyEvents);

      if (steps.length > 0) {
        const placed = classifyTopLevelMessages(bodyEvents, steps);
        messageNodes.push(
          m(ProgressBlock, {
            key: `progress-${sectionUserEvent.event_id}`,
            tasks: steps,
            body_events: bodyEvents,
            toolResults,
            leading_messages: placed.leading,
            interstep_messages: placed.inter_step,
            trailing_messages: placed.trailing,
            stophook_messages: placeStopHookChips(bodyEvents, steps),
            agentId,
          }),
        );
      } else if (bodyEvents.length > 0) {
        // No step records were declared for this turn. Render the body as
        // plain chat -- the agent's prose and tool-call blocks inline, the
        // same way assistant messages render outside a progress section --
        // rather than wrapping it in a pseudo-progress "ungrouped work"
        // block. Tool_result events are looked up via the prebuilt
        // toolResults map by renderAssistantMessage, so only the
        // assistant_messages need to be emitted here. Stop-hook chips are
        // interleaved at their chronological position (bodyEvents is ordered).
        for (const e of bodyEvents) {
          if (e.type === "assistant_message") {
            messageNodes.push(renderAssistantMessage(e, toolResults, agentId));
          } else if (e.type === "user_message" && isStopHookFeedback(e.content ?? "")) {
            const chipNode = renderUserMessage(e);
            if (chipNode !== null) messageNodes.push(chipNode);
          }
        }
      }
    }

    for (const event of visibleEvents) {
      if (event.type === "user_message") {
        if (isNonBoundaryUserMessage(event.content ?? "")) {
          if (sectionUserEvent === null) {
            // No section open yet to attach it to -- render at top level.
            const chipNode = renderUserMessage(event);
            if (chipNode !== null) messageNodes.push(chipNode);
          } else {
            // Defer rendering: flushSection places the chip at its
            // chronological position inside the section (woven into the
            // timeline, or interleaved in the no-steps plain-chat fallback)
            // so it no longer floats above the whole turn.
            bodyEvents.push(event);
          }
        } else {
          flushSection(event.timestamp);
          sectionUserEvent = event;
          sectionStart = event.timestamp;
          bodyEvents = [];
          const userNode = renderUserMessage(event);
          if (userNode !== null) messageNodes.push(userNode);
        }
      } else if (event.type === "assistant_message") {
        if (sectionUserEvent === null) {
          messageNodes.push(renderAssistantMessage(event, toolResults, agentId));
        } else {
          bodyEvents.push(event);
        }
      } else if (event.type === "tool_result") {
        bodyEvents.push(event);
      }
    }
    flushSection("");

    return m("div", { class: "message-list-wrapper" }, [
      m(
        "div",
        { class: "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6" },
        messageNodes,
      ),
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
