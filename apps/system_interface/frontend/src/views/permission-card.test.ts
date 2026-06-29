import type m from "mithril";
import { describe, expect, it, vi } from "vitest";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import type { ScopeInfo } from "./latchkey-scope-info";
import type { PermissionResolution } from "./message-classification";
import { openPermissionRequest, parsePermissionRequest, renderPermissionCard } from "./permission-card";

function makeToolCall(inputPreview: string): ToolCall {
  return {
    tool_call_id: "call-1",
    tool_name: "Bash",
    input_preview: inputPreview,
  };
}

function makeResult(output: string, isError = false): ToolResultEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "tool_result",
    event_id: "evt-result-1",
    source: "session",
    message_uuid: "uuid-1",
    tool_call_id: "call-1",
    tool_name: "Bash",
    output,
    is_error: isError,
  };
}

// Mirror the `PermissionCard` component's pre-render work so each test exercises
// the pure renderer exactly as the live card calls it: parse the request once,
// assemble the raw-request text, and pass in an injected `scopeInfo` (instead of
// driving the async gateway lookup).
function renderCardFor(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
  resolution: PermissionResolution | null = null,
  scopeInfo: ScopeInfo | null = null,
): m.Vnode {
  const details = parsePermissionRequest(toolCall, toolResult);
  const rawInput = toolCall.input_preview || "";
  const rawOutput = toolResult?.output || "";
  const rawText = rawOutput ? `${rawInput}\n\n${rawOutput}` : rawInput;
  return renderPermissionCard(details, scopeInfo, resolution, rawText);
}

// A realistic input_preview: the command is JSON-encoded and may be truncated
// at 200 chars, but the reserved host appears near the start.
const PERMISSION_INPUT = JSON.stringify({
  command:
    "latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \\\n  -H 'Content-Type: application/json' \\\n  -d '{...}'",
});

// A realistic output: curl writes a progress meter to stderr/stdout before the
// JSON body, so the whole thing is not directly JSON-parseable. The body
// carries the rich fields the card surfaces (rationale, request_type, payload).
const PERMISSION_OUTPUT = `  % Total    % Received % Xferd
100  1007  100   670  100   337
{
  "request_id": "885711ec07bf47239d71294e1534330b",
  "agent_id": "agent-28dc23edadd34caeaba58441ac8e7218",
  "rationale": "I need to read #eng-releases to summarize the deploy thread.",
  "request_type": "predefined",
  "payload": { "scope": "slack-api", "permissions": ["slack-read-all"] }
}`;

// A file-sharing request: payload carries a path and access mode instead.
const FILE_SHARING_OUTPUT = `{"request_id":"fs-1","rationale":"write the report locally","request_type":"file-sharing","payload":{"path":"/Users/you/Documents/report","access":"WRITE"}}`;

describe("parsePermissionRequest", () => {
  it("parses the rich details of a successful predefined creation POST", () => {
    const result = parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));
    expect(result).toEqual({
      requestId: "885711ec07bf47239d71294e1534330b",
      requestType: "predefined",
      rationale: "I need to read #eng-releases to summarize the deploy thread.",
      scope: "slack-api",
      permissions: ["slack-read-all"],
      path: null,
      access: null,
    });
  });

  it("parses a file-sharing request's path and access mode", () => {
    const result = parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult(FILE_SHARING_OUTPUT));
    expect(result).toMatchObject({
      requestId: "fs-1",
      requestType: "file-sharing",
      path: "/Users/you/Documents/report",
      access: "WRITE",
      scope: null,
    });
  });

  it("ignores tool calls that are not permission-request POSTs", () => {
    const unrelated = makeToolCall(JSON.stringify({ command: "ls -la" }));
    expect(parsePermissionRequest(unrelated, makeResult("anything"))).toBeNull();
  });

  it("ignores reads of the latchkey permissions endpoints (non-POST host)", () => {
    const read = makeToolCall(
      JSON.stringify({ command: "latchkey curl http://latchkey-self.invalid/permissions/self" }),
    );
    expect(parsePermissionRequest(read, makeResult('{"rules": []}'))).toBeNull();
  });

  it("returns null while the tool result is still pending", () => {
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), null)).toBeNull();
  });

  it("returns null when the creation call errored", () => {
    const errored = makeResult("request not permitted by the user", true);
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), errored)).toBeNull();
  });

  it("returns null when the output has no JSON body", () => {
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult("nope"))).toBeNull();
  });

  it("returns null when the JSON body has no request_id", () => {
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult('{"agent_id":"a"}'))).toBeNull();
  });
});

// Depth-first search for the first vnode matching a predicate.
function findVnode(
  node: unknown,
  pred: (v: { tag?: unknown }) => boolean,
): { tag?: unknown; children?: unknown } | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const hit = findVnode(child, pred);
      if (hit) return hit;
    }
    return null;
  }
  if (node !== null && typeof node === "object") {
    const vnode = node as { tag?: unknown; children?: unknown };
    if (pred(vnode)) return vnode;
    return findVnode(vnode.children, pred);
  }
  return null;
}

// The exact text of the first text vnode (tag "#") under a node, or null.
function textOf(node: unknown): string | null {
  const t = findVnode(node, (v) => v.tag === "#" && typeof (v as { children?: unknown }).children === "string");
  return t ? (t.children as string) : null;
}

describe("renderPermissionCard", () => {
  it("heads the card and shows the request and a button", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));

    const title = findVnode(
      vnode,
      (v) =>
        v.tag === "span" && (v as { attrs?: { className?: string } }).attrs?.className === "permission-request-title",
    );
    // The predefined service name is conveyed by the scope on the "Requesting"
    // line until the gateway catalog resolves a friendly name into the heading.
    expect(textOf(title)).toBe("Permission request");

    // The "Requesting" value is shown: the permission and scope as separate
    // no-wrap tokens (so a long name can't break mid-name).
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "slack-read-all"),
    ).not.toBeNull();
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "slack-api"),
    ).not.toBeNull();

    // The agent's reason for the request is surfaced on the card.
    expect(
      findVnode(
        vnode,
        (v) =>
          v.tag === "#" &&
          (v as { children?: unknown }).children === "I need to read #eng-releases to summarize the deploy thread.",
      ),
    ).not.toBeNull();

    const button = findVnode(vnode, (v) => v.tag === "button");
    expect(button).not.toBeNull();
    expect(textOf(button)).toBe("Review & respond");
  });

  it("wires the button to open the modal with the request id", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));
    const button = findVnode(vnode, (v) => v.tag === "button") as { attrs?: { onclick?: (e: Event) => void } } | null;

    const postMessage = vi.fn();
    vi.stubGlobal("window", { parent: { postMessage } });
    try {
      button?.attrs?.onclick?.({ preventDefault() {}, stopPropagation() {} } as unknown as Event);
    } finally {
      vi.unstubAllGlobals();
    }
    expect(postMessage).toHaveBeenCalledWith(
      { type: "minds:open-request-modal", requestId: "885711ec07bf47239d71294e1534330b" },
      "*",
    );
  });

  it("shows a pending state with no button before the result arrives", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), null);

    const title = findVnode(
      vnode,
      (v) =>
        v.tag === "span" && (v as { attrs?: { className?: string } }).attrs?.className === "permission-request-title",
    );
    expect(textOf(title)).toBe("Permission request");
    expect(findVnode(vnode, (v) => v.tag === "button")).toBeNull();
  });

  it("shows a Granted verdict and no review button once granted", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), "granted");
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Granted"),
    ).not.toBeNull();
    // The action button is replaced by the verdict.
    expect(findVnode(vnode, (v) => v.tag === "button")).toBeNull();
  });

  it("shows a Denied verdict once denied", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), "denied");
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Denied"),
    ).not.toBeNull();
    expect(findVnode(vnode, (v) => v.tag === "button")).toBeNull();
  });

  it("shows a couldn't-complete verdict for an error outcome", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), "error");
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Couldn't complete"),
    ).not.toBeNull();
    expect(findVnode(vnode, (v) => v.tag === "button")).toBeNull();
  });

  it("uses the gateway service name in the title and adds a permission tooltip", () => {
    const scopeInfo: ScopeInfo = {
      scope: "slack-api",
      display_name: "Slack",
      description: "Any interaction with the Slack API.",
      permissions: [{ name: "slack-read-all", description: "All read operations across the Slack API." }],
    };
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), null, scopeInfo);

    const title = findVnode(
      vnode,
      (v) =>
        v.tag === "span" && (v as { attrs?: { className?: string } }).attrs?.className === "permission-request-title",
    );
    expect(textOf(title)).toBe("Permission request: Slack");

    // The requested permission is a hoverable span carrying its description.
    const perm = findVnode(
      vnode,
      (v) =>
        v.tag === "span" && (v as { attrs?: { className?: string } }).attrs?.className === "permission-request-perm",
    ) as { attrs?: Record<string, unknown> } | null;
    expect(perm).not.toBeNull();
    expect(perm?.attrs?.["data-tooltip"]).toBe("All read operations across the Slack API.");
    expect(textOf(perm)).toBe("slack-read-all");
  });

  it("falls back to the plain scope title and text before the catalog resolves", () => {
    // No scopeInfo (e.g. the lookup hasn't landed): title stays generic and the
    // permission shows as a plain no-wrap token with no hoverable tooltip span.
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));
    const title = findVnode(
      vnode,
      (v) =>
        v.tag === "span" && (v as { attrs?: { className?: string } }).attrs?.className === "permission-request-title",
    );
    expect(textOf(title)).toBe("Permission request");
    expect(
      findVnode(
        vnode,
        (v) =>
          v.tag === "span" && (v as { attrs?: { className?: string } }).attrs?.className === "permission-request-perm",
      ),
    ).toBeNull();
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "slack-read-all"),
    ).not.toBeNull();
  });
});

describe("openPermissionRequest", () => {
  it("posts the open-request-modal message to the parent window", () => {
    // The chat UI runs inside an iframe; vitest's node environment has no
    // `window`, so stand one in with a spy parent.
    const postMessage = vi.fn();
    vi.stubGlobal("window", { parent: { postMessage } });
    try {
      openPermissionRequest("req-123");
    } finally {
      vi.unstubAllGlobals();
    }
    expect(postMessage).toHaveBeenCalledWith({ type: "minds:open-request-modal", requestId: "req-123" }, "*");
  });
});
