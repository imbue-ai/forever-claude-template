import { describe, expect, it, vi } from "vitest";
import type { ToolCall, TranscriptEvent, ToolResultEvent } from "../models/Response";
import {
  buildToolResultsWithSkillExpansions,
  parsePermissionRequest,
  renderPermissionRequestFooter,
  openPermissionRequest,
  renderSubagentCard,
} from "./message-renderers";
import { isSkillExpansionUserMessage } from "./user-message-classification";

// Avoid importing the heavy/DOM-dependent module graph (dockview, dompurify) at test time;
// renderSubagentCard only needs openSubagentTab, and the card path never calls MarkdownContent.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({ MarkdownContent: () => null }));

function skillToolCall(ts: string, callId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "test-model",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: "Skill", input_preview: "{}" }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

function toolResult(ts: string, callId: string, output: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "test-tool",
    output,
    is_error: false,
  };
}

function skillExpansion(ts: string, skillName: string, eventId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "user_message",
    event_id: eventId,
    source: "test",
    role: "user",
    content: `Base directory for this skill: /home/.claude/skills/${skillName}/\n\n# ${skillName}\n\nBody of ${skillName}.`,
  };
}

describe("isSkillExpansionUserMessage", () => {
  it("matches user_messages whose content starts with the skill-expansion preamble", () => {
    expect(isSkillExpansionUserMessage("Base directory for this skill: /x")).toBe(true);
    expect(isSkillExpansionUserMessage("hello")).toBe(false);
    expect(isSkillExpansionUserMessage("Stop hook feedback:\n...")).toBe(false);
  });
});

describe("buildToolResultsWithSkillExpansions", () => {
  it("folds a skill-expansion user_message into the matching Skill tool call's output", () => {
    const events = [
      skillToolCall("2026-04-28T01:00:00Z", "tc-skill"),
      toolResult("2026-04-28T01:00:01Z", "tc-skill", "Loading skill..."),
      skillExpansion("2026-04-28T01:00:02Z", "build-web-service", "u-exp"),
    ];
    const results = buildToolResultsWithSkillExpansions(events);
    const skillResult = results.get("tc-skill");
    expect(skillResult).toBeDefined();
    expect(skillResult?.output).toContain("Loading skill...");
    expect(skillResult?.output).toContain("Base directory for this skill:");
    expect(skillResult?.output).toContain("# build-web-service");
  });

  it("creates a synthetic tool_result if the Skill tool call has no explicit result", () => {
    const events = [
      skillToolCall("2026-04-28T01:00:00Z", "tc-skill"),
      skillExpansion("2026-04-28T01:00:01Z", "frontend-design", "u-exp"),
    ];
    const results = buildToolResultsWithSkillExpansions(events);
    const skillResult = results.get("tc-skill");
    expect(skillResult).toBeDefined();
    expect(skillResult?.output).toContain("# frontend-design");
    expect(skillResult?.tool_call_id).toBe("tc-skill");
  });

  it("matches two back-to-back Skill calls to their respective expansions in order", () => {
    const events = [
      skillToolCall("2026-04-28T01:00:00Z", "tc-1"),
      skillExpansion("2026-04-28T01:00:01Z", "alpha", "u-1"),
      skillToolCall("2026-04-28T01:00:02Z", "tc-2"),
      skillExpansion("2026-04-28T01:00:03Z", "beta", "u-2"),
    ];
    const results = buildToolResultsWithSkillExpansions(events);
    expect(results.get("tc-1")?.output).toContain("# alpha");
    expect(results.get("tc-1")?.output).not.toContain("# beta");
    expect(results.get("tc-2")?.output).toContain("# beta");
    expect(results.get("tc-2")?.output).not.toContain("# alpha");
  });

  it("matches two Skill calls inside one assistant_message to expansions in order", () => {
    // Claude may emit multiple parallel tool_use blocks in a single
    // assistant_message. Each Skill call must get its own expansion.
    const events: TranscriptEvent[] = [
      {
        timestamp: "2026-04-28T01:00:00Z",
        type: "assistant_message",
        event_id: "a-multi",
        source: "test",
        model: "test-model",
        text: "",
        tool_calls: [
          { tool_call_id: "tc-a", tool_name: "Skill", input_preview: "{}" },
          { tool_call_id: "tc-b", tool_name: "Skill", input_preview: "{}" },
        ],
        stop_reason: null,
        usage: null,
        is_auth_error: false,
      },
      skillExpansion("2026-04-28T01:00:01Z", "alpha", "u-a"),
      skillExpansion("2026-04-28T01:00:02Z", "beta", "u-b"),
    ];
    const results = buildToolResultsWithSkillExpansions(events);
    expect(results.get("tc-a")?.output).toContain("# alpha");
    expect(results.get("tc-a")?.output).not.toContain("# beta");
    expect(results.get("tc-b")?.output).toContain("# beta");
    expect(results.get("tc-b")?.output).not.toContain("# alpha");
  });

  it("keeps earlier Skill calls queued when a later Skill call appears before any expansion", () => {
    // Two assistant_messages each issue one Skill call, then two
    // expansions arrive. The first expansion must match the first Skill
    // call, not the most recent one.
    const events: TranscriptEvent[] = [
      skillToolCall("2026-04-28T01:00:00Z", "tc-first"),
      skillToolCall("2026-04-28T01:00:01Z", "tc-second"),
      skillExpansion("2026-04-28T01:00:02Z", "first-skill", "u-1"),
      skillExpansion("2026-04-28T01:00:03Z", "second-skill", "u-2"),
    ];
    const results = buildToolResultsWithSkillExpansions(events);
    expect(results.get("tc-first")?.output).toContain("# first-skill");
    expect(results.get("tc-second")?.output).toContain("# second-skill");
  });

  it("leaves non-Skill tool_results alone", () => {
    const events = [
      {
        timestamp: "2026-04-28T01:00:00Z",
        type: "assistant_message" as const,
        event_id: "a-1",
        source: "test",
        model: "test-model",
        text: "",
        tool_calls: [{ tool_call_id: "tc-read", tool_name: "Read", input_preview: "" }],
        stop_reason: null,
        usage: null,
        is_auth_error: false,
      },
      toolResult("2026-04-28T01:00:01Z", "tc-read", "file contents"),
    ];
    const results = buildToolResultsWithSkillExpansions(events);
    expect(results.get("tc-read")?.output).toBe("file contents");
  });
});

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
