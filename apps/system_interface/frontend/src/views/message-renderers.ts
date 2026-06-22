/**
 * Shared rendering functions for transcript events.
 * Used by both ChatPanel and SubagentView.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import type {
  TranscriptEvent,
  AssistantMessageEvent,
  UserMessageEvent,
  ToolResultEvent,
  ToolCall,
} from "../models/Response";
import { openSubagentTab } from "./DockviewWorkspace";
import type { PermissionResolution } from "./message-classification";
import {
  isCollapsibleUserMessage,
  isHiddenUserMessage,
  isPermissionRequestCall,
  isSkillExpansionUserMessage,
} from "./message-classification";
import { PermissionCard } from "./permission-card";

/** Build a tool_call_id -> tool_result map, merging skill-expansion
 *  user_messages into the output of their preceding "Skill" tool call so
 *  the SKILL.md body renders inside the same dropdown rather than as a
 *  separate inline chip. */
export function buildToolResultsWithSkillExpansions(events: TranscriptEvent[]): Map<string, ToolResultEvent> {
  const sorted = [...events].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  const toolResults = new Map<string, ToolResultEvent>();
  for (const e of sorted) {
    if (e.type === "tool_result" && e.tool_call_id) {
      toolResults.set(e.tool_call_id, e);
    }
  }
  // Walk chronologically. Skill tool_call_ids are queued in FIFO order as
  // they appear and matched to skill-expansion user_messages in the same
  // order. A single assistant_message may carry multiple Skill calls; a
  // second assistant_message may queue another Skill call before any
  // expansion for the previous one has arrived. In both cases each
  // expansion must land on the right call, hence a queue rather than a
  // single "most recent" slot.
  const pendingSkillCallIds: string[] = [];
  for (const e of sorted) {
    if (e.type === "assistant_message" && e.tool_calls) {
      for (const tc of e.tool_calls) {
        if (tc.tool_name === "Skill") {
          pendingSkillCallIds.push(tc.tool_call_id);
        }
      }
      continue;
    }
    if (e.type === "user_message" && isSkillExpansionUserMessage(e.content ?? "") && pendingSkillCallIds.length > 0) {
      const targetCallId = pendingSkillCallIds.shift() as string;
      const existing = toolResults.get(targetCallId);
      const expansion = e.content ?? "";
      const baseOutput = existing?.output ?? "";
      const mergedOutput = baseOutput ? `${baseOutput}\n\n${expansion}` : expansion;
      if (existing) {
        toolResults.set(targetCallId, { ...existing, output: mergedOutput });
      } else {
        toolResults.set(targetCallId, {
          timestamp: e.timestamp,
          type: "tool_result",
          event_id: `skill-expansion-${targetCallId}`,
          source: e.source,
          tool_call_id: targetCallId,
          tool_name: "Skill",
          output: mergedOutput,
          is_error: false,
        });
      }
    }
  }
  return toolResults;
}

/**
 * Hide auth-error turns from the pre-login prefix once login has recovered.
 *
 * A fresh chat with no Claude credentials produces a run of "Not logged in"
 * assistant messages before the user authenticates. Once login succeeds and
 * /welcome is resent, the first visible turn should be the friendly greeting,
 * not the prior failed attempts.
 *
 * Restricted to the PREFIX of the transcript (turns that occurred before any
 * successful assistant message). A mid-session token expiration -- where the
 * user has already had successful exchanges before the auth error -- is left
 * intact, since the user may want to scroll back to see what they were doing.
 */
export function computeAuthErrorHiddenEventIds(events: TranscriptEvent[]): Set<string> {
  const hidden = new Set<string>();

  let firstSuccessIdx = -1;
  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    if (ev.type === "assistant_message" && ev.is_auth_error !== true) {
      firstSuccessIdx = i;
      break;
    }
  }
  if (firstSuccessIdx === -1) return hidden;

  for (let i = 0; i < firstSuccessIdx; i++) {
    const ev = events[i];
    if (ev.type !== "assistant_message" || ev.is_auth_error !== true) continue;
    hidden.add(ev.event_id);
    for (let j = i - 1; j >= 0; j--) {
      const prev = events[j];
      if (prev.type === "user_message") {
        hidden.add(prev.event_id);
        break;
      }
      if (prev.type === "assistant_message") break;
    }
  }

  return hidden;
}

export function StableUserMessage(): m.Component<{ event: UserMessageEvent }> {
  let renderedEventId: string | null = null;
  return {
    onbeforeupdate(vnode) {
      return vnode.attrs.event.event_id !== renderedEventId;
    },
    view(vnode) {
      const event = vnode.attrs.event;
      renderedEventId = event.event_id;
      const content = event.content || "";
      const collapsible = isCollapsibleUserMessage(content);

      if (collapsible) {
        return m("div", { class: "tool-call-block" }, [
          m(
            "div",
            {
              class: "tool-call-header",
              onclick(e: Event) {
                const block = (e.currentTarget as HTMLElement).parentElement;
                if (block) {
                  block.classList.toggle("tool-call-block--expanded");
                }
              },
            },
            [m("span", { class: "tool-call-chevron" }, "\u25B8"), m("span", collapsible.label)],
          ),
          m("div", { class: "tool-call-details" }, [
            m("div", { class: "tool-call-input" }, [m("pre", m("code", content))]),
          ]),
        ]);
      }

      return m("div", { class: "message-user-bubble" }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, content),
      ]);
    },
  };
}

export function renderUserMessage(event: UserMessageEvent): m.Vnode | null {
  const content = event.content || "";
  if (isHiddenUserMessage(content)) {
    return null;
  }
  const collapsible = isCollapsibleUserMessage(content);
  const messageClass = collapsible ? "message message-system-collapsed" : "message message-user";
  // id mirrors the assistant rows so the virtualized list can measure every
  // rendered row's height by querying ``.message-list > [id]``.
  return m("div", { id: event.event_id, class: messageClass, key: event.event_id }, [m(StableUserMessage, { event })]);
}

export function countResolvedToolResults(
  toolCalls: ToolCall[] | undefined,
  toolResults: Map<string, ToolResultEvent>,
): number {
  if (!toolCalls) return 0;
  let count = 0;
  for (const tc of toolCalls) {
    if (toolResults.has(tc.tool_call_id)) count++;
  }
  return count;
}

export function countSubagentCards(toolCalls: ToolCall[] | undefined): number {
  if (!toolCalls) return 0;
  let count = 0;
  for (const tc of toolCalls) {
    if (tc.subagent_metadata) count++;
  }
  return count;
}

export function StableAssistantMessage(): m.Component<{
  event: AssistantMessageEvent;
  toolResults: Map<string, ToolResultEvent>;
  agentId: string;
}> {
  let renderedEventId: string | null = null;
  let renderedToolResultCount = 0;
  let renderedSubagentCardCount = 0;
  return {
    onbeforeupdate(vnode) {
      const { event, toolResults } = vnode.attrs;
      const currentToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      // A subagent card can appear after the message was first rendered: the
      // backend re-broadcasts the parent with subagent_metadata once a running
      // subagent's linkage lands. Repaint when that count grows so the plain
      // tool-call block upgrades to the rich card.
      const currentSubagentCardCount = countSubagentCards(event.tool_calls);
      return (
        event.event_id !== renderedEventId ||
        currentToolResultCount !== renderedToolResultCount ||
        currentSubagentCardCount !== renderedSubagentCardCount
      );
    },
    view(vnode) {
      const event = vnode.attrs.event;
      const toolResults = vnode.attrs.toolResults;
      const agentId = vnode.attrs.agentId;
      renderedEventId = event.event_id;
      renderedToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      renderedSubagentCardCount = countSubagentCards(event.tool_calls);

      return m("div", renderAssistantMessageChildren(event, toolResults, agentId));
    },
  };
}

export function renderAssistantMessage(
  event: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  agentId: string,
): m.Vnode {
  return m(
    "div",
    {
      id: event.event_id,
      class: "message message-assistant",
      key: event.event_id,
    },
    m(StableAssistantMessage, { event, toolResults, agentId }),
  );
}

export function renderSubagentCard(toolCall: ToolCall, agentId: string, isRunning: boolean): m.Vnode {
  const metadata = toolCall.subagent_metadata;
  // Description and agent type come from the tool call itself, so the card renders fully
  // even before the subagent session is linked; fall back to metadata if the tool input
  // fields are absent (older events).
  const description = toolCall.description || metadata?.description || "Sub-agent";
  const agentType = toolCall.subagent_type || metadata?.agent_type || "";
  const sessionId = metadata?.session_id;

  // The header status indicator communicates whether the sub-agent is still working: a pulsing
  // green dot while the Agent call is in flight (no tool result yet), switching to a muted
  // checkmark -- like a completed progress step -- once the sub-agent finishes. On completion the
  // whole card also drops its green accent for neutral grey, since green reads as "active".
  const statusIndicator = isRunning
    ? m("span", {
        class: "subagent-card-status-dot subagent-card-status-dot--running",
        title: "Working",
        "aria-label": "Sub-agent is working",
      })
    : m(
        "svg.subagent-card-status-check",
        {
          width: 16,
          height: 16,
          viewBox: "0 0 16 16",
          fill: "none",
          title: "Finished",
          "aria-label": "Sub-agent finished",
        },
        // Same filled-circle-with-check mark used for a done step in the progress timeline.
        m.trust(
          '<circle cx="8" cy="8" r="7" fill="currentColor"/>' +
            '<path d="M4.5 8L7 10.5L11.5 6" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
        ),
      );

  return m("div", { class: `subagent-card${isRunning ? "" : " subagent-card--done"}` }, [
    m("div", { class: "subagent-card-header" }, [
      statusIndicator,
      m("span", { class: "subagent-card-description" }, description),
      agentType ? m("span", { class: "subagent-card-type-badge" }, agentType) : null,
    ]),
    // The click-through needs the subagent session_id, which only arrives once the call is
    // linked. The label stays "View conversation" throughout so it doesn't flip-flop; before
    // the session is known it renders as a muted, non-clickable placeholder (there is no
    // conversation to open yet), becoming an active link the moment linkage lands.
    sessionId
      ? m(
          "a",
          {
            class: "subagent-card-link",
            href: "javascript:void(0)",
            onclick(e: Event) {
              e.preventDefault();
              e.stopPropagation();
              openSubagentTab(agentId, sessionId, description);
            },
          },
          "View conversation",
        )
      : m("span", { class: "subagent-card-link subagent-card-link--pending" }, "View conversation"),
  ]);
}

export function renderToolCallBlock(toolCall: ToolCall, toolResult: ToolResultEvent | null): m.Vnode {
  const headerText = `Tool: ${toolCall.tool_name}`;
  const inputText = toolCall.input_preview || "";
  const outputText = toolResult?.output || "";
  const isError = toolResult?.is_error === true;

  return m("div", { class: "tool-call-block" }, [
    m(
      "div",
      {
        class: "tool-call-header",
        onclick(e: Event) {
          const block = (e.currentTarget as HTMLElement).parentElement;
          if (block) {
            block.classList.toggle("tool-call-block--expanded");
          }
        },
      },
      [m("span", { class: "tool-call-chevron" }, "\u25B8"), m("span", headerText)],
    ),
    m("div", { class: "tool-call-details" }, [
      inputText ? m("div", { class: "tool-call-input" }, [m("pre", m("code", inputText))]) : null,
      outputText
        ? m("div", { class: isError ? "tool-call-output tool-call-output--error" : "tool-call-output" }, [
            m("pre", m("code", outputText)),
          ])
        : null,
    ]),
  ]);
}

/**
 * Render the children (text + tool calls) of an assistant message.
 * Used by both the stable (memoized) and simple assistant message renderers.
 */
export function renderAssistantMessageChildren(
  event: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  agentId: string,
  permissionResolution: PermissionResolution | null = null,
): m.Children[] {
  const textContent = event.text || "";
  const toolCalls = event.tool_calls || [];

  const children: m.Children[] = [];
  if (textContent) {
    children.push(m(MarkdownContent, { content: textContent }));
  }
  for (const toolCall of toolCalls) {
    // Render the rich card as soon as we have the Agent call's description (from the tool
    // input), even before its subagent session is linked; the card shows a non-clickable
    // "Running…" state until subagent_metadata.session_id arrives.
    if (toolCall.tool_name === "Agent" && (toolCall.subagent_metadata || toolCall.description)) {
      // The Agent call's tool result arrives only when the sub-agent finishes, so its
      // absence is our signal that the sub-agent is still actively working.
      const subagentRunning = !toolResults.has(toolCall.tool_call_id);
      children.push(renderSubagentCard(toolCall, agentId, subagentRunning));
      continue;
    }
    const result = toolResults.get(toolCall.tool_call_id) ?? null;
    // A permission request renders as its own card (the request, a verdict or
    // button, and the raw call) rather than a generic tool block.
    // Gated on the input-only predicate so the card shows even while the request
    // is still pending -- the same signal the timeline walk uses to lift it out
    // of its step. The resolution (once the user decides) comes from the walk.
    if (isPermissionRequestCall(toolCall)) {
      children.push(m(PermissionCard, { toolCall, toolResult: result, resolution: permissionResolution }));
      continue;
    }
    children.push(renderToolCallBlock(toolCall, result));
  }
  return children;
}

/**
 * Render a permission-break timeline item: the issuing assistant message (its
 * prose plus the permission card), with the user's granted/denied verdict
 * threaded into the card. Used by the timeline renderers for the `permission`
 * item; goes direct (not via the memoized StableAssistantMessage) so the
 * resolution reaches the card.
 */
export function renderPermissionItem(
  event: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  agentId: string,
  resolution: PermissionResolution | null,
): m.Vnode {
  return m(
    "div",
    { id: event.event_id, class: "message message-assistant", key: event.event_id },
    renderAssistantMessageChildren(event, toolResults, agentId, resolution),
  );
}
