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
  isPermissionRequestCall,
  isSkillExpansionUserMessage,
} from "./message-classification";

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

export function renderSubagentCard(toolCall: ToolCall, agentId: string): m.Vnode {
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
    // linked. Until then show a non-clickable "running" state so the card is still rich.
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
      : m("span", { class: "subagent-card-link subagent-card-link--pending" }, "Running…"),
  ]);
}

/** The rich fields a created permission request echoes back on stdout, parsed
 *  from the tool result. `requestId` is always present (it's what the modal
 *  button needs); the rest depend on the request type. */
export interface PermissionRequestDetails {
  requestId: string;
  /** "predefined" (a service scope) or "file-sharing", or null if absent. */
  requestType: string | null;
  /** The agent's human-readable reason for the request. */
  rationale: string | null;
  /** Predefined requests: the latchkey scope (e.g. "slack-api") and the
   *  specific permissions being granted. */
  scope: string | null;
  permissions: string[];
  /** File-sharing requests: the path and access mode (READ/WRITE). */
  path: string | null;
  access: string | null;
}

/** Human-readable service names keyed by latchkey scope, mirroring the latchkey
 *  services catalog (libs/mngr_latchkey .../services.json). The catalog lives in
 *  the gateway, not the frontend, so this is a display-only copy; an unknown
 *  scope falls back to a title-cased form via {@link serviceDisplayName}. */
const SERVICE_DISPLAY_NAMES: Record<string, string> = {
  aws: "AWS",
  "calendly-api": "Calendly",
  "coolify-api": "Coolify",
  "discord-api": "Discord",
  "dropbox-api": "Dropbox",
  "figma-api": "Figma",
  "github-git": "GitHub (git)",
  "github-rest-api": "GitHub (REST API)",
  "gitlab-api": "GitLab (REST API)",
  "gitlab-git": "GitLab (git)",
  "google-analytics-api": "Google Analytics",
  "google-calendar-api": "Google Calendar",
  "google-directions-api": "Google Directions",
  "google-docs-api": "Google Docs",
  "google-drive-api": "Google Drive",
  "google-gmail-api": "Gmail",
  "google-people-api": "Google Contacts",
  "google-sheets-api": "Google Sheets",
  "linear-api": "Linear",
  "mailchimp-api": "Mailchimp",
  "notion-api": "Notion",
  "sentry-api": "Sentry",
  "slack-api": "Slack",
  "stripe-api": "Stripe",
  "telegram-api": "Telegram",
  "umami-api": "Umami",
  "yelp-api": "Yelp",
  "zoom-api": "Zoom",
};

/** The display name for a latchkey scope: the catalog name, or a title-cased
 *  fallback (strip a trailing `-api`/`-git`, capitalize words) so a scope the
 *  catalog copy doesn't know still reads reasonably. */
export function serviceDisplayName(scope: string): string {
  const known = SERVICE_DISPLAY_NAMES[scope];
  if (known) return known;
  return scope
    .replace(/-(api|git)$/, "")
    .split("-")
    .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : word))
    .join(" ");
}

/**
 * Parse the rich details of a *successful* latchkey permission-request creation
 * call out of its tool result.
 *
 * An agent asks the user for permission by POSTing to the reserved
 * `latchkey-self.invalid/permission-requests` host (see the latchkey skill).
 * The created request's JSON -- request_id, rationale, request_type, and a
 * type-specific payload -- is echoed back on stdout (after curl's progress
 * meter), so the result output contains a JSON object starting at the first
 * `{`.
 *
 * Returns the parsed details when the call is such a creation POST that
 * succeeded and carries a request_id; otherwise null (the request is still
 * pending, errored, or the output wasn't parseable -- the caller then shows a
 * pending card and keeps the raw output available).
 */
export function parsePermissionRequest(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
): PermissionRequestDetails | null {
  // The same input-only predicate the timeline walk uses to lift the request
  // out of its step, so the two stay in lockstep.
  if (!isPermissionRequestCall(toolCall)) {
    return null;
  }
  if (!toolResult || toolResult.is_error === true) {
    return null;
  }
  const output = toolResult.output || "";
  // curl writes a progress meter before the response body; the JSON object is
  // the last thing on stdout, starting at the first `{`.
  const start = output.indexOf("{");
  if (start < 0) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(output.slice(start));
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.request_id !== "string") {
    return null;
  }
  const payload =
    typeof obj.payload === "object" && obj.payload !== null ? (obj.payload as Record<string, unknown>) : {};
  const permissions = Array.isArray(payload.permissions)
    ? payload.permissions.filter((p): p is string => typeof p === "string")
    : [];
  return {
    requestId: obj.request_id,
    requestType: typeof obj.request_type === "string" ? obj.request_type : null,
    rationale: typeof obj.rationale === "string" ? obj.rationale : null,
    scope: typeof payload.scope === "string" ? payload.scope : null,
    permissions,
    path: typeof payload.path === "string" ? payload.path : null,
    access: typeof payload.access === "string" ? payload.access : null,
  };
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

/** The card heading: "Permission request: <service>" for a predefined request,
 *  "Permission request: File access" for a file-sharing one, or plain
 *  "Permission request" when the subject isn't known (e.g. still pending). */
function permissionHeading(details: PermissionRequestDetails | null): string {
  if (details === null) return "Permission request";
  if (details.requestType === "file-sharing") return "Permission request: File access";
  if (details.scope) return `Permission request: ${serviceDisplayName(details.scope)}`;
  return "Permission request";
}

/** The concise "what is being requested" line: the permissions on a service
 *  scope, or an access mode on a path. Null when there's nothing specific to
 *  show. */
function permissionRequesting(details: PermissionRequestDetails | null): string | null {
  if (details === null) return null;
  if (details.scope) {
    const perms = details.permissions.join(", ");
    return perms ? `${perms} on ${details.scope}` : details.scope;
  }
  if (details.path) {
    return `${details.access ?? "access"} on ${details.path}`;
  }
  return null;
}

/**
 * Render an agent permission request as a card: the service it touches, the
 * agent's rationale, what's being requested, a button that opens the modal, and
 * a disclosure preserving the raw request/response. Replaces the generic
 * "Tool: Bash" block so the request reads as what it is.
 *
 * Before the result lands (still pending) the details are null: the card shows
 * a waiting state with no button. The button appears once the result carries a
 * request_id.
 */
export function renderPermissionRequestBlock(toolCall: ToolCall, toolResult: ToolResultEvent | null): m.Vnode {
  const details = parsePermissionRequest(toolCall, toolResult);
  const requesting = permissionRequesting(details);
  const rawInput = toolCall.input_preview || "";
  const rawOutput = toolResult?.output || "";
  const rawText = rawOutput ? `${rawInput}\n\n${rawOutput}` : rawInput;

  return m("div", { class: "permission-request" }, [
    m("div", { class: "permission-request-heading" }, [
      renderLockIcon(),
      m("span", { class: "permission-request-title" }, permissionHeading(details)),
    ]),
    details === null
      ? m(
          "div",
          { class: "permission-request-rationale permission-request-rationale--pending" },
          "Waiting for the request to register…",
        )
      : details.rationale
        ? m("div", { class: "permission-request-rationale" }, details.rationale)
        : null,
    requesting
      ? m("div", { class: "permission-request-detail" }, [
          m("span", { class: "permission-request-detail-label" }, "Requesting"),
          m("code", { class: "permission-request-detail-value" }, requesting),
        ])
      : null,
    details !== null
      ? m("div", { class: "permission-request-actions" }, [
          m(
            "button",
            {
              class: "permission-request-button",
              type: "button",
              onclick(e: Event) {
                e.preventDefault();
                e.stopPropagation();
                openPermissionRequest(details.requestId);
              },
            },
            [renderLockIcon(), m("span", "Review & respond")],
          ),
        ])
      : null,
    rawText
      ? m("details", { class: "permission-request-raw" }, [
          m("summary", "Show raw request"),
          m("pre", m("code", rawText)),
        ])
      : null,
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
    // Render the rich card as soon as we have the Agent call's description (from the tool
    // input), even before its subagent session is linked; the card shows a non-clickable
    // "Running…" state until subagent_metadata.session_id arrives.
    if (toolCall.tool_name === "Agent" && (toolCall.subagent_metadata || toolCall.description)) {
      children.push(renderSubagentCard(toolCall, agentId));
      continue;
    }
    const result = toolResults.get(toolCall.tool_call_id) ?? null;
    // A permission request renders as its own card (service, rationale, the
    // request, a button, and the raw call) rather than a generic tool block.
    // Gated on the input-only predicate so the card shows even while the request
    // is still pending -- the same signal the timeline walk uses to lift it out
    // of its step.
    if (isPermissionRequestCall(toolCall)) {
      children.push(renderPermissionRequestBlock(toolCall, result));
      continue;
    }
    children.push(renderToolCallBlock(toolCall, result));
  }
  return children;
}
