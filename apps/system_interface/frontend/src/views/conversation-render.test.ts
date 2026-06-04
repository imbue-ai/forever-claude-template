import { describe, expect, it, vi } from "vitest";

// Mithril schedules redraws via requestAnimationFrame at import time; the node
// test env has none. Polyfill before importing anything that pulls in mithril.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import type m from "mithril";
import type {
  TranscriptEvent,
  AssistantMessageEvent,
  UserMessageEvent,
  ToolResultEvent,
  StepEnrichment,
} from "../models/Response";
import { renderConversation, isSubagentRunning } from "./conversation-render";
import { ProgressBlock } from "./ProgressBlock";
import type { StepNode, TimelineItem } from "./turn-grouping";

function userMsg(ts: string, content: string, id = `u-${ts}`): UserMessageEvent {
  return { timestamp: ts, type: "user_message", event_id: id, source: "test", role: "user", content };
}

function assistantText(ts: string, text: string, stop_reason: string | null = "end_turn"): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${ts}`,
    source: "test",
    model: "m",
    text,
    tool_calls: [],
    stop_reason,
    usage: null,
    is_auth_error: false,
  };
}

function tkMsg(
  ts: string,
  command: string,
  callId: string,
  stop_reason: string | null = "tool_use",
): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: "Bash", input_preview: `{"command":"${command}"}` }],
    stop_reason,
    usage: null,
    is_auth_error: false,
  };
}

function result(ts: string, callId: string, output: string): ToolResultEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "Bash",
    output,
    is_error: false,
  };
}

function enrich(entries: Record<string, Partial<StepEnrichment>>): Map<string, StepEnrichment> {
  const map = new Map<string, StepEnrichment>();
  for (const [id, e] of Object.entries(entries)) {
    map.set(id, {
      title: e.title ?? id,
      summary: e.summary ?? null,
      status: e.status ?? "in_progress",
      created_at: e.created_at ?? "2026-04-28T01:00:00.000000Z",
    });
  }
  return map;
}

// Mithril vnodes are inspected structurally in tests (see message-renderers.test.ts):
// `tag` may be a string or a component reference, so type it loosely.
type VNodeLike = { tag?: unknown; attrs?: unknown; children?: unknown };

/** Pull the message nodes out of the wrapper renderConversation returns. */
function messageNodes(vnode: m.Vnode): VNodeLike[] {
  const wrapper = vnode as unknown as VNodeLike;
  const inner = (wrapper.children as VNodeLike[])[0];
  return inner.children as VNodeLike[];
}

describe("renderConversation (shared by main chat and subagent view)", () => {
  it("renders a subagent's own steps as a progress timeline, not raw tk Bash calls", () => {
    // A subagent transcript: it created, started, did work in, and closed its
    // own step. Its `Updated <id> ->` transitions live in this (subagent)
    // stream, and its enrichment is scoped to the subagent. The same renderer
    // the main chat uses must surface this as a step timeline.
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "the prompt"),
      tkMsg("2026-04-28T01:00:01Z", "tk start cod-sub1", "c-start"),
      result("2026-04-28T01:00:02Z", "c-start", "Updated cod-sub1 -> in_progress"),
      tkMsg("2026-04-28T01:00:03Z", "tk close cod-sub1 done", "c-close"),
      result("2026-04-28T01:00:04Z", "c-close", "Updated cod-sub1 -> closed"),
      assistantText("2026-04-28T01:00:05Z", "All finished."),
    ];
    const enrichment = enrich({ "cod-sub1": { title: "Explore the code", status: "closed", summary: "explored it" } });

    const nodes = messageNodes(renderConversation(events, enrichment, "agent-x", /* agentIsIdle */ true));

    // A ProgressBlock (the timeline) is present, carrying the subagent's step
    // with its enriched title/summary -- not a bare Bash tool-call block.
    const progress = nodes.find((n) => n.tag === ProgressBlock);
    expect(progress).toBeDefined();
    const items = (progress?.attrs as { items: TimelineItem[] }).items;
    const steps = items.filter((i): i is { kind: "step"; step: StepNode } => i.kind === "step").map((i) => i.step);
    expect(steps).toHaveLength(1);
    expect(steps[0].ticket_id).toBe("cod-sub1");
    expect(steps[0].title).toBe("Explore the code");
    expect(steps[0].status).toBe("done");
    expect(steps[0].summary).toBe("explored it");
  });

  it("falls back to plain chat when the conversation has no steps", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "hi"),
      assistantText("2026-04-28T01:00:01Z", "hello"),
    ];
    const nodes = messageNodes(renderConversation(events, new Map(), "agent-x", true));
    expect(nodes.find((n) => n.tag === ProgressBlock)).toBeUndefined();
  });
});

describe("isSubagentRunning", () => {
  it("is running while the last assistant turn is mid-tool-use or unstopped", () => {
    expect(isSubagentRunning([assistantText("2026-04-28T01:00:00Z", "", "tool_use")])).toBe(true);
    expect(isSubagentRunning([assistantText("2026-04-28T01:00:00Z", "", null)])).toBe(true);
  });

  it("is settled once the last assistant turn stops terminally", () => {
    expect(isSubagentRunning([assistantText("2026-04-28T01:00:00Z", "done", "end_turn")])).toBe(false);
  });

  it("is settled when there is no assistant output yet", () => {
    expect(isSubagentRunning([userMsg("2026-04-28T01:00:00Z", "go")])).toBe(false);
  });
});
