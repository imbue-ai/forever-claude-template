import { describe, expect, it, vi } from "vitest";
import type { ToolCall, TranscriptEvent } from "../models/Response";
import { buildToolResultsWithSkillExpansions, renderSubagentCard } from "./message-renderers";
import { isSkillExpansionUserMessage, parsePermissionResolution } from "./message-classification";

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

describe("parsePermissionResolution", () => {
  it("reads the verdict from the injected granted/denied notifications", () => {
    expect(
      parsePermissionResolution(
        "Your permission request for Slack was granted with the following permissions: slack-read-all. Please retry the call that was blocked.",
      ),
    ).toBe("granted");
    expect(
      parsePermissionResolution("Your permission request for Slack was denied. Do not retry the blocked call."),
    ).toBe("denied");
    expect(
      parsePermissionResolution(
        "Your read & write file-sharing permission request for '/Users/you/Documents/report' was granted. Please retry the call that was blocked.",
      ),
    ).toBe("granted");
    // A request that could not be completed is an "error", not a deny decision.
    expect(
      parsePermissionResolution(
        "Your permission request for Google Drive could not be completed because the user's sign-in flow did not finish. Do not retry yet; report this to the user.",
      ),
    ).toBe("error");
  });

  it("ignores ordinary user messages", () => {
    expect(parsePermissionResolution("can you grant me access to slack?")).toBeNull();
    expect(parsePermissionResolution("Your permission request looks good")).toBeNull();
    expect(parsePermissionResolution("")).toBeNull();
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
    const vnode = renderSubagentCard(toolCall, "agent-1", true);
    const text = allText(vnode);
    const classes = collectClasses(vnode).join(" ");

    expect(text).toContain("explore foo");
    expect(text).toContain("Explore");
    // Not yet linked: the label is the muted, non-clickable "View conversation" placeholder.
    expect(text).toContain("View conversation");
    expect(classes).toContain("subagent-card-link--pending");
  });

  it("shows a pulsing running dot on a green card while the sub-agent is working", () => {
    const toolCall: ToolCall = {
      tool_call_id: "t1",
      tool_name: "Agent",
      input_preview: "{}",
      description: "explore foo",
      subagent_type: "Explore",
      subagent_metadata: { agent_type: "Explore", description: "explore foo", session_id: "agent-sub1" },
    };
    const classes = collectClasses(renderSubagentCard(toolCall, "agent-1", true)).join(" ");
    expect(classes).toContain("subagent-card-status-dot--running");
    expect(classes).not.toContain("subagent-card-status-check");
    expect(classes).not.toContain("subagent-card--done");
  });

  it("switches to a checkmark and greys the card once the sub-agent finishes", () => {
    const toolCall: ToolCall = {
      tool_call_id: "t1",
      tool_name: "Agent",
      input_preview: "{}",
      description: "explore foo",
      subagent_type: "Explore",
      subagent_metadata: { agent_type: "Explore", description: "explore foo", session_id: "agent-sub1" },
    };
    const classes = collectClasses(renderSubagentCard(toolCall, "agent-1", false)).join(" ");
    expect(classes).toContain("subagent-card-status-check");
    expect(classes).toContain("subagent-card--done");
    expect(classes).not.toContain("subagent-card-status-dot--running");
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
    const vnode = renderSubagentCard(toolCall, "agent-1", false);
    const text = allText(vnode);
    const classes = collectClasses(vnode).join(" ");

    expect(text).toContain("View conversation");
    // Linked: the active link, not the muted pending placeholder.
    expect(classes).not.toContain("subagent-card-link--pending");
  });

  it("falls back to subagent_metadata fields when the tool call lacks description", () => {
    const toolCall: ToolCall = {
      tool_call_id: "t1",
      tool_name: "Agent",
      input_preview: "{}",
      subagent_metadata: { agent_type: "Explore", description: "from metadata", session_id: "agent-sub1" },
    };
    const text = allText(renderSubagentCard(toolCall, "agent-1", false));
    expect(text).toContain("from metadata");
    expect(text).toContain("View conversation");
  });
});

// Walk a mithril vnode tree and collect every element's class string, so tests can assert
// on structural state (e.g. the running vs done status indicator) that allText can't see.
function collectClasses(node: unknown): string[] {
  if (node == null) return [];
  if (Array.isArray(node)) return node.flatMap(collectClasses);
  if (typeof node === "object") {
    // Mithril normalizes the `class` hyperscript attr into `className` on the vnode.
    const v = node as { attrs?: { className?: unknown }; children?: unknown };
    const own = typeof v.attrs?.className === "string" ? [v.attrs.className] : [];
    return [...own, ...collectClasses(v.children)];
  }
  return [];
}
