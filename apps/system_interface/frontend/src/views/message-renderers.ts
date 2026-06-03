/**
 * Shared rendering functions for transcript events.
 * Used by both ChatPanel and SubagentView.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import type { TranscriptEvent, ToolCall } from "../models/Response";
import { openSubagentTab } from "./DockviewWorkspace";

export function isCollapsibleUserMessage(content: string): { label: string } | null {
  if (content.startsWith("Stop hook feedback:\n")) {
    return { label: "Stop hook feedback" };
  }
  if (content.startsWith("Base directory for this skill:")) {
    const match = content.match(/skills\/([^\n/]+)/);
    return { label: match ? `Skill: ${match[1]}` : "Skill expansion" };
  }
  return null;
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

export function isHiddenUserMessage(content: string): boolean {
  // The minds desktop client seeds every new agent with "/welcome" as its
  // initial message so the welcome skill can produce a friendly greeting.
  // Claude Code expands that invocation into TWO transcript events:
  //   1. the invocation itself, whose content wraps "/welcome" in
  //      <command-name>.../</command-name> (plus a <command-message>...),
  //   2. the skill expansion, which starts with
  //      "Base directory for this skill: .../skills/welcome/..." and
  //      carries the SKILL.md body.
  // Hide both so the first visible turn is just the assistant's greeting.
  // Restricted to the welcome skill specifically -- any OTHER slash
  // command the user later runs still renders normally.
  if (content.includes("<command-name>/welcome</command-name>")) {
    return true;
  }
  if (content.startsWith("Base directory for this skill:") && /skills\/welcome(\/|\b)/.test(content)) {
    return true;
  }
  return false;
}

export function StableUserMessage(): m.Component<{ event: TranscriptEvent }> {
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

export function renderUserMessage(event: TranscriptEvent): m.Vnode | null {
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
  toolResults: Map<string, TranscriptEvent>,
): number {
  if (!toolCalls) return 0;
  let count = 0;
  for (const tc of toolCalls) {
    if (toolResults.has(tc.tool_call_id)) count++;
  }
  return count;
}

export function StableAssistantMessage(): m.Component<{
  event: TranscriptEvent;
  toolResults: Map<string, TranscriptEvent>;
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
  event: TranscriptEvent,
  toolResults: Map<string, TranscriptEvent>,
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
  toolResult: TranscriptEvent | null,
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
  toolResult: TranscriptEvent | null,
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
  event: TranscriptEvent,
  toolResults: Map<string, TranscriptEvent>,
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
      continue;
    }
    const result = toolResults.get(toolCall.tool_call_id) ?? null;
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
