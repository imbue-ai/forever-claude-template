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

export function countInterruptedSubagentCards(toolCalls: ToolCall[] | undefined): number {
  if (!toolCalls) return 0;
  let count = 0;
  for (const tc of toolCalls) {
    if (tc.is_interrupted) count++;
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
  let renderedInterruptedCardCount = 0;
  return {
    onbeforeupdate(vnode) {
      const { event, toolResults } = vnode.attrs;
      const currentToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      // A subagent card can change after the message was first rendered: the
      // backend re-broadcasts the parent with subagent_metadata once a running
      // subagent's linkage lands, or with is_interrupted once a stop kills a
      // delegation that never linked. Repaint when either count grows so the
      // card upgrades (rich link / "Stopped") instead of staying "Running…".
      const currentSubagentCardCount = countSubagentCards(event.tool_calls);
      const currentInterruptedCardCount = countInterruptedSubagentCards(event.tool_calls);
      return (
        event.event_id !== renderedEventId ||
        currentToolResultCount !== renderedToolResultCount ||
        currentSubagentCardCount !== renderedSubagentCardCount ||
        currentInterruptedCardCount !== renderedInterruptedCardCount
      );
    },
    view(vnode) {
      const event = vnode.attrs.event;
      const toolResults = vnode.attrs.toolResults;
      const agentId = vnode.attrs.agentId;
      renderedEventId = event.event_id;
      renderedToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      renderedSubagentCardCount = countSubagentCards(event.tool_calls);
      renderedInterruptedCardCount = countInterruptedSubagentCards(event.tool_calls);

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

/**
 * Pick the non-clickable status label for a subagent card with no session to
 * open. "Running…" only while the delegation can actually still be running: a
 * tool_result (the delegation ended -- finished without a recoverable
 * transcript, was denied, or errored) or the backend's is_interrupted mark
 * (the issuing Claude process was killed, e.g. by the stop button, before any
 * linkage or result could land) are both terminal.
 */
function subagentCardStatusLabel(toolCall: ToolCall, result: ToolResultEvent | null): string {
  if (result !== null && !result.is_error) return "Finished";
  if (result !== null || toolCall.is_interrupted) return "Stopped";
  return "Running…";
}

export function renderSubagentCard(toolCall: ToolCall, agentId: string, result: ToolResultEvent | null): m.Vnode {
  const metadata = toolCall.subagent_metadata;
  // Description and agent type come from the tool call itself, so the card renders fully
  // even before the subagent session is linked; fall back to metadata if the tool input
  // fields are absent (older events).
  const description = toolCall.description || metadata?.description || "Sub-agent";
  const agentType = toolCall.subagent_type || metadata?.agent_type || "";
  const sessionId = metadata?.session_id;

  return m("div", { class: "subagent-card" }, [
    m("div", { class: "subagent-card-header" }, [
      m("span", { class: "subagent-card-description" }, description),
      agentType ? m("span", { class: "subagent-card-type-badge" }, agentType) : null,
    ]),
    // The click-through needs the subagent session_id, which only arrives once the call is
    // linked. Until then show a non-clickable status so the card is still rich.
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
      : m(
          "span",
          { class: "subagent-card-link subagent-card-link--pending" },
          subagentCardStatusLabel(toolCall, result),
        ),
  ]);
}

/**
 * Detect a *successful* latchkey permission-request creation call and pull the
 * resulting request id out of the tool result.
 *
 * An agent asks the user for permission by POSTing to the reserved
 * `latchkey-self.invalid/permission-requests` host (see the latchkey skill).
 * The created request's JSON -- including a `request_id` -- is echoed back on
 * stdout, so the tool result output contains a `"request_id": "..."` field.
 *
 * Returns the request id when the tool call is such a creation POST and it
 * succeeded; otherwise null (in which case the caller renders the raw tool
 * block, so failures/errors stay visible and debuggable).
 */
export function parsePermissionRequest(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
): { requestId: string } | null {
  // The command is JSON-encoded inside input_preview; the reserved host is
  // short enough to survive the 200-char preview truncation.
  const input = toolCall.input_preview || "";
  if (!input.includes("latchkey-self.invalid/permission-requests")) {
    return null;
  }
  // Only a creation (POST) yields a request_id; reads of existing permissions
  // hit different endpoints and are excluded by the host check above anyway.
  if (!/-X\s*POST|--request\s*POST/i.test(input)) {
    return null;
  }
  if (!toolResult || toolResult.is_error === true) {
    return null;
  }
  const output = toolResult.output || "";
  const match = output.match(/"request_id"\s*:\s*"([^"]+)"/);
  if (!match) {
    return null;
  }
  return { requestId: match[1] };
}

/**
 * Ask the outer Minds app to open its permission-request modal. The chat UI
 * runs inside an iframe, so we hand the request id to the parent via
 * postMessage rather than rendering the modal ourselves.
 */
export function openPermissionRequest(requestId: string): void {
  window.parent.postMessage({ type: "minds:open-request-modal", requestId }, "*");
}

/** Small lock glyph for the permission-request footer button. */
function renderLockIcon(): m.Vnode {
  return m(
    "svg",
    {
      class: "permission-request-icon",
      width: "14",
      height: "14",
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    [m("rect", { x: "3", y: "11", width: "18", height: "11", rx: "2" }), m("path", { d: "M7 11V7a5 5 0 0 1 10 0v4" })],
  );
}

/**
 * Footer rendered inside a permission-request tool block: an outlined button
 * that opens the modal. Living inside the block (rather than alongside it)
 * ties the affordance visually to the request that created it.
 */
export function renderPermissionRequestFooter(requestId: string): m.Vnode {
  return m("div", { class: "tool-call-permission-footer" }, [
    m(
      "button",
      {
        class: "permission-request-button",
        type: "button",
        onclick(e: Event) {
          e.preventDefault();
          e.stopPropagation();
          openPermissionRequest(requestId);
        },
      },
      [renderLockIcon(), m("span", "Permission request")],
    ),
  ]);
}

export function renderToolCallBlock(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
  footer: m.Vnode | null = null,
): m.Vnode {
  const headerText = `Tool: ${toolCall.tool_name}`;
  const inputText = toolCall.input_preview || "";
  const outputText = toolResult?.output || "";
  const isError = toolResult?.is_error === true;
  const blockClass = footer ? "tool-call-block tool-call-block--permission-request" : "tool-call-block";

  return m("div", { class: blockClass }, [
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
    footer,
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
    const result = toolResults.get(toolCall.tool_call_id) ?? null;
    // Render the rich card as soon as we have the Agent call's description (from the tool
    // input), even before its subagent session is linked; the card shows a non-clickable
    // status until subagent_metadata.session_id arrives (or the delegation terminates
    // without one -- see subagentCardStatusLabel).
    if (toolCall.tool_name === "Agent" && (toolCall.subagent_metadata || toolCall.description)) {
      children.push(renderSubagentCard(toolCall, agentId, result));
      continue;
    }
    // For a successful permission request, render the button as a footer inside
    // the tool block so the affordance is visually bound to the request. The
    // block stays in place (the footer is appended within it once the result
    // arrives), so the layout doesn't jump.
    const permissionRequest = parsePermissionRequest(toolCall, result);
    const footer = permissionRequest ? renderPermissionRequestFooter(permissionRequest.requestId) : null;
    children.push(renderToolCallBlock(toolCall, result, footer));
  }
  return children;
}
