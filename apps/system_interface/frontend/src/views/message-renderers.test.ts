import { describe, expect, it, vi } from "vitest";

// Avoid importing the heavy/DOM-dependent module graph (dockview, dompurify) at test time;
// renderSubagentCard only needs openSubagentTab, and the card path never calls MarkdownContent.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({ MarkdownContent: () => null }));

import { renderSubagentCard } from "./message-renderers";
import type { ToolCall } from "../models/Response";

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
