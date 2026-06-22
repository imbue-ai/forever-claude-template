/**
 * The agent permission-request card: a self-contained component that renders a
 * latchkey permission request as a human-readable card (what's being requested,
 * the agent's reason, a review button or the user's verdict, and a raw-request
 * disclosure) rather than a generic "Tool: Bash" block.
 *
 * The card has one seam: `PermissionCard` is the live component (it parses the
 * request once and looks up the gateway catalog so a scope like `slack-api`
 * shows as "Slack"), and `renderPermissionCard` is the pure renderer it delegates
 * to once details and scope info are in hand. Keeping the pure renderer separate
 * lets tests inject `scopeInfo` synchronously without driving the async lookup.
 */

import m from "mithril";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import type { ScopeInfo } from "./latchkey-scope-info";
import { getScopeInfo } from "./latchkey-scope-info";
import type { PermissionResolution } from "./message-classification";
import { isPermissionRequestCall } from "./message-classification";

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

/** Small lock glyph shown in the permission-request card heading and button. */
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

/** The card heading: "Permission request: File access" for a file-sharing
 *  request; "Permission request: <service>" for a predefined request once the
 *  gateway catalog has resolved the scope to a friendly service name; otherwise
 *  plain "Permission request" (the scope still shows on the "Requesting" line). */
function permissionHeading(details: PermissionRequestDetails | null, scopeInfo: ScopeInfo | null): string {
  if (details?.requestType === "file-sharing") return "Permission request: File access";
  if (details?.scope && scopeInfo) return `Permission request: ${scopeInfo.display_name}`;
  return "Permission request";
}

/** A hyphenated token (a permission name, scope, or path) wrapped so it never
 *  breaks mid-name -- line breaks fall between tokens, not inside them. */
function renderRequestToken(text: string): m.Vnode {
  return m("span", { class: "permission-request-token" }, text);
}

/** A requested permission name that reveals its description in a CSS tooltip
 *  centered over the name on hover/focus. `data-tooltip` carries the description
 *  (the bubble is `::after content: attr(data-tooltip)`) and doubles as the
 *  accessible label. Also a no-break token (see `.permission-request-perm`). */
function renderPermissionName(name: string, description: string): m.Vnode {
  return m("span", { class: "permission-request-perm", "data-tooltip": description, tabindex: "0" }, name);
}

/** The value for the "Requesting" line: the permissions on a service scope, or
 *  an access mode on a path. Each permission name and the scope/path render as
 *  no-break tokens so a long hyphenated name never wraps mid-name; once the
 *  gateway catalog resolves, a described permission also becomes hoverable for
 *  its description. Null when there's nothing specific to show. */
function permissionRequestingValue(
  details: PermissionRequestDetails | null,
  scopeInfo: ScopeInfo | null,
): m.Children | null {
  if (details === null) return null;
  if (details.scope) {
    if (details.permissions.length === 0) return renderRequestToken(details.scope);
    const nodes: m.Children[] = [];
    details.permissions.forEach((name, index) => {
      if (index > 0) nodes.push(", ");
      const description = scopeInfo?.permissions.find((permission) => permission.name === name)?.description ?? null;
      nodes.push(description ? renderPermissionName(name, description) : renderRequestToken(name));
    });
    nodes.push(" on ");
    nodes.push(renderRequestToken(details.scope));
    return nodes;
  }
  if (details.path) {
    return [`${details.access ?? "access"} on `, renderRequestToken(details.path)];
  }
  return null;
}

/** A small glyph for the resolved-request verdict: a check (granted), a cross
 *  (denied), or an exclamation (error / couldn't complete). */
function renderVerdictIcon(resolution: PermissionResolution): m.Vnode {
  const path =
    resolution === "granted"
      ? "M4.5 8.5L7 11L11.5 5.5"
      : resolution === "denied"
        ? "M5 5l6 6M11 5l-6 6"
        : "M8 4v5M8 11.5h0";
  return m(
    "svg",
    {
      class: "permission-request-verdict-icon",
      width: "14",
      height: "14",
      viewBox: "0 0 16 16",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    m("path", { d: path }),
  );
}

/** The label shown beside the verdict icon. "error" reads as "Couldn't
 *  complete" -- the request didn't finish, distinct from a deny decision. */
function verdictLabel(resolution: PermissionResolution): string {
  if (resolution === "granted") return "Granted";
  if (resolution === "denied") return "Denied";
  return "Couldn't complete";
}

/** The resolved verdict badge shown in place of the action button once the
 *  request is resolved (granted, denied, or could-not-complete). */
function renderPermissionVerdict(resolution: PermissionResolution): m.Vnode {
  return m("div", { class: `permission-request-verdict permission-request-verdict--${resolution}` }, [
    renderVerdictIcon(resolution),
    m("span", verdictLabel(resolution)),
  ]);
}

/**
 * Pure renderer for the permission card, given the already-parsed request
 * `details`, the resolved gateway `scopeInfo` (or null before it lands), the
 * user's `resolution` (or null while pending), and the `rawText` for the
 * disclosure. The live `PermissionCard` component computes these once and calls
 * here; tests call it directly with an injected `scopeInfo`.
 *
 * `resolution` reflects the user's decision once it lands: the action button is
 * replaced by a Granted/Denied verdict. Before the result lands (still pending)
 * `details` is null: the card shows a waiting state with no button. The button
 * appears once the result carries a request_id and the request is still
 * awaiting a decision.
 */
export function renderPermissionCard(
  details: PermissionRequestDetails | null,
  scopeInfo: ScopeInfo | null,
  resolution: PermissionResolution | null,
  rawText: string,
): m.Vnode {
  const requesting = permissionRequestingValue(details, scopeInfo);

  return m("div", { class: "permission-request" }, [
    m("div", { class: "permission-request-heading" }, [
      renderLockIcon(),
      m("span", { class: "permission-request-title" }, permissionHeading(details, scopeInfo)),
    ]),
    resolution === null && details === null
      ? m("div", { class: "permission-request-status" }, "Waiting for the request to register…")
      : null,
    requesting
      ? m("div", { class: "permission-request-detail" }, [
          m("span", { class: "permission-request-detail-label" }, "Requesting"),
          m("code", { class: "permission-request-detail-value" }, requesting),
        ])
      : null,
    details?.rationale
      ? m("div", { class: "permission-request-detail permission-request-reason" }, [
          m("span", { class: "permission-request-detail-label" }, "Reason"),
          m("span", { class: "permission-request-reason-value" }, details.rationale),
        ])
      : null,
    resolution !== null
      ? m("div", { class: "permission-request-actions" }, renderPermissionVerdict(resolution))
      : details !== null
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

/**
 * The live permission-request card. Parses the request once, resolves its
 * service scope to the gateway catalog (service display name + permission
 * descriptions) -- a cache-guarded async lookup, so the card first renders with
 * the raw scope and updates once the catalog resolves -- and delegates to
 * `renderPermissionCard`. Predefined requests have a scope to resolve;
 * file-sharing requests don't.
 */
export function PermissionCard(): m.Component<{
  toolCall: ToolCall;
  toolResult: ToolResultEvent | null;
  resolution: PermissionResolution | null;
}> {
  return {
    view(vnode) {
      const { toolCall, toolResult, resolution } = vnode.attrs;
      const details = parsePermissionRequest(toolCall, toolResult);
      const scopeInfo = details?.scope ? getScopeInfo(details.scope) : null;
      const rawInput = toolCall.input_preview || "";
      const rawOutput = toolResult?.output || "";
      const rawText = rawOutput ? `${rawInput}\n\n${rawOutput}` : rawInput;
      return renderPermissionCard(details, scopeInfo, resolution, rawText);
    },
  };
}
