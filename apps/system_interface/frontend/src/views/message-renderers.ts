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
import {
  isCollapsibleUserMessage,
  isHiddenUserMessage,
  isSkillExpansionUserMessage,
} from "./user-message-classification";

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
  return m("div", { class: messageClass, key: event.event_id }, [m(StableUserMessage, { event })]);
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

export function StableAssistantMessage(): m.Component<{
  event: AssistantMessageEvent;
  toolResults: Map<string, ToolResultEvent>;
  agentId: string;
}> {
  let renderedEventId: string | null = null;
  let renderedToolResultCount = 0;
  return {
    onbeforeupdate(vnode) {
      const { event, toolResults } = vnode.attrs;
      const currentToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      return event.event_id !== renderedEventId || currentToolResultCount !== renderedToolResultCount;
    },
    view(vnode) {
      const event = vnode.attrs.event;
      const toolResults = vnode.attrs.toolResults;
      const agentId = vnode.attrs.agentId;
      renderedEventId = event.event_id;
      renderedToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);

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

export function renderSubagentCard(toolCall: ToolCall, agentId: string): m.Vnode {
  const metadata = toolCall.subagent_metadata;
  if (!metadata) {
    return renderToolCallBlock(toolCall, null);
  }

  const description = metadata.description || "Sub-agent";
  const agentType = metadata.agent_type || "";

  return m("div", { class: "subagent-card" }, [
    m("div", { class: "subagent-card-header" }, [
      m("span", { class: "subagent-card-description" }, description),
      agentType ? m("span", { class: "subagent-card-type-badge" }, agentType) : null,
    ]),
    m(
      "a",
      {
        class: "subagent-card-link",
        href: "javascript:void(0)",
        onclick(e: Event) {
          e.preventDefault();
          e.stopPropagation();
          openSubagentTab(agentId, metadata.session_id, description);
        },
      },
      "View conversation",
    ),
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
): m.Children[] {
  const textContent = event.text || "";
  const toolCalls = event.tool_calls || [];

  const children: m.Children[] = [];
  if (textContent) {
    children.push(m(MarkdownContent, { content: textContent }));
  }
  for (const toolCall of toolCalls) {
    if (toolCall.tool_name === "Agent" && toolCall.subagent_metadata) {
      children.push(renderSubagentCard(toolCall, agentId));
    } else {
      const result = toolResults.get(toolCall.tool_call_id) ?? null;
      children.push(renderToolCallBlock(toolCall, result));
    }
  }
  return children;
}
