import { describe, expect, it, vi } from "vitest";

// Avoid importing the heavy/DOM-dependent module graph (dockview, dompurify) at test time;
// renderSubagentCard only needs openSubagentTab, and the card path never calls MarkdownContent.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({ MarkdownContent: () => null }));

import {
  openPermissionRequest,
  parsePermissionRequest,
  renderPermissionRequestFooter,
  renderSubagentCard,
} from "./message-renderers";
import type { ToolCall, TranscriptEvent } from "../models/Response";

// Recursively gather every string in a Mithril vnode tree (text + children).
function allText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(allText).join(" ");
  if (typeof node === "object") {
    const v = node as { text?: unknown; children?: unknown };
    return `${allText(v.text)} ${allText(v.children)}`;
  }
  return "";
}

describe("renderSubagentCard", () => {
  it("renders a rich card from the tool call alone, with a non-clickable pending state", () => {
    const toolCall: ToolCall = {
      tool_call_id: "t1",
      tool_name: "Agent",
      input_preview: "{}",
      description: "explore foo",
      subagent_type: "Explore",
    };
    const vnode = renderSubagentCard(toolCall, "agent-1");
    const text = allText(vnode);

    expect(text).toContain("explore foo");
    expect(text).toContain("Explore");
    // Not yet linked: shows the running placeholder, not a clickable conversation link.
    expect(text).toContain("Running");
    expect(text).not.toContain("View conversation");
  });

  it("renders a clickable conversation link once the subagent session is linked", () => {
    const toolCall: ToolCall = {
      tool_call_id: "t1",
      tool_name: "Agent",
      input_preview: "{}",
      description: "explore foo",
      subagent_type: "Explore",
      subagent_metadata: { agent_type: "Explore", description: "explore foo", session_id: "agent-sub1" },
    };
    const vnode = renderSubagentCard(toolCall, "agent-1");
    const text = allText(vnode);

    expect(text).toContain("View conversation");
    expect(text).not.toContain("Running");
  });

  it("falls back to subagent_metadata fields when the tool call lacks description", () => {
    const toolCall: ToolCall = {
      tool_call_id: "t1",
      tool_name: "Agent",
      input_preview: "{}",
      subagent_metadata: { agent_type: "Explore", description: "from metadata", session_id: "agent-sub1" },
    };
    const text = allText(renderSubagentCard(toolCall, "agent-1"));
    expect(text).toContain("from metadata");
    expect(text).toContain("View conversation");
  });
});

function makeToolCall(inputPreview: string): ToolCall {
  return {
    tool_call_id: "call-1",
    tool_name: "Bash",
    input_preview: inputPreview,
  };
}

function makeResult(output: string, isError = false): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "tool_result",
    event_id: "evt-result-1",
    source: "session",
    message_uuid: "uuid-1",
    tool_call_id: "call-1",
    output,
    is_error: isError,
  };
}

// A realistic input_preview: the command is JSON-encoded and may be truncated
// at 200 chars, but the reserved host appears near the start.
const PERMISSION_INPUT = JSON.stringify({
  command:
    "latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \\\n  -H 'Content-Type: application/json' \\\n  -d '{...}'",
});

// A realistic output: curl writes a progress meter to stderr/stdout before the
// JSON body, so the whole thing is not directly JSON-parseable.
const PERMISSION_OUTPUT = `  % Total    % Received % Xferd
100  1007  100   670  100   337
{
  "request_id": "885711ec07bf47239d71294e1534330b",
  "agent_id": "agent-28dc23edadd34caeaba58441ac8e7218",
  "request_type": "predefined"
}`;

describe("parsePermissionRequest", () => {
  it("extracts the request id from a successful creation POST", () => {
    const result = parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));
    expect(result).toEqual({ requestId: "885711ec07bf47239d71294e1534330b" });
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

  it("returns null when the output has no request_id", () => {
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult("nope"))).toBeNull();
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

describe("renderPermissionRequestFooter", () => {
  it("renders a footer div containing a 'Permission request' button", () => {
    const vnode = renderPermissionRequestFooter("req-123") as unknown as {
      tag: string;
      attrs: { className: string };
    };
    expect(vnode.tag).toBe("div");
    // Mithril normalizes the `class` attr to `className`.
    expect(vnode.attrs.className).toBe("tool-call-permission-footer");

    const button = findVnode(vnode, (v) => v.tag === "button");
    expect(button).not.toBeNull();
    // Mithril wraps the label string in a text vnode (tag "#").
    const label = findVnode(button, (v) => v.tag === "#");
    expect(label?.children).toBe("Permission request");
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
