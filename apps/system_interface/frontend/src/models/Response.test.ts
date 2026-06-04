import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. The default (node) test environment has no such global, which would
// make the `m.redraw()` calls inside appendEvents throw. Provide a polyfill
// before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import {
  appendEvents,
  applyEnrichmentSnapshot,
  getEnrichmentForAgent,
  getEventsForAgent,
  type AssistantMessageEvent,
  type StepEnrichment,
  type ToolCall,
  type TranscriptEvent,
} from "./Response";

function assistantWithAgentToolCall(
  eventId: string,
  toolCallId: string,
  metadata?: { agent_type: string; description: string; session_id: string },
): AssistantMessageEvent {
  return {
    timestamp: "2026-01-01T00:00:01Z",
    type: "assistant_message",
    event_id: eventId,
    source: "claude/common_transcript",
    message_uuid: eventId,
    model: "test-model",
    text: "",
    tool_calls: [
      {
        tool_call_id: toolCallId,
        tool_name: "Agent",
        input_preview: "{}",
        ...(metadata ? { subagent_metadata: metadata } : {}),
      },
    ],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

// getEventsForAgent returns the TranscriptEvent union; narrow to the assistant
// variant before touching tool_calls (the discriminated-union contract).
function toolCallsOf(event: TranscriptEvent): ToolCall[] {
  if (event.type !== "assistant_message") {
    throw new Error(`expected assistant_message, got ${event.type}`);
  }
  return event.tool_calls;
}

describe("appendEvents subagent_metadata merge", () => {
  it("merges late subagent_metadata onto an already-stored assistant message", () => {
    const agentId = "agent-merge";
    const metadata = { agent_type: "Explore", description: "explore foo", session_id: "agent-sub1" };

    // Parent Agent tool_call streamed before its subagent linkage was known.
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);
    const before = getEventsForAgent(agentId);
    expect(before).toHaveLength(1);
    expect(toolCallsOf(before[0])[0].subagent_metadata).toBeUndefined();

    // Backend re-broadcasts the same event (same event_id) once linkage lands.
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1", metadata)]);

    const after = getEventsForAgent(agentId);
    // Still a single message -- the re-broadcast must not be appended as a duplicate.
    expect(after).toHaveLength(1);
    expect(toolCallsOf(after[0])[0].subagent_metadata).toEqual(metadata);
  });

  it("ignores a re-broadcast that carries no new metadata", () => {
    const agentId = "agent-noop";
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);

    const events = getEventsForAgent(agentId);
    expect(events).toHaveLength(1);
    expect(toolCallsOf(events[0])[0].subagent_metadata).toBeUndefined();
  });

  it("still appends genuinely new events", () => {
    const agentId = "agent-append";
    appendEvents(agentId, [assistantWithAgentToolCall("ev-1", "toolu_1")]);
    appendEvents(agentId, [assistantWithAgentToolCall("ev-2", "toolu_2")]);

    expect(getEventsForAgent(agentId)).toHaveLength(2);
  });
});

function step(title: string, status: StepEnrichment["status"] = "open"): StepEnrichment {
  return { title, summary: null, status, created_at: "2026-04-28T01:00:00.000000Z" };
}

describe("enrichment scope keying", () => {
  it("keeps a subagent's steps out of the main view's enrichment table", () => {
    const agentId = "agent-scope";
    const subSession = "agent-sub1";

    // The backend serves the main scope under no session id and the subagent
    // scope tagged with its session id; the frontend stores them separately.
    applyEnrichmentSnapshot(agentId, { "cod-main": step("Main step") });
    applyEnrichmentSnapshot(agentId, { "cod-sub": step("Sub step") }, subSession);

    // Main view (no session id): only the main step -- the subagent's pending
    // step does NOT leak in, so it cannot appear in the main pending roster.
    const main = getEnrichmentForAgent(agentId);
    expect([...main.keys()]).toEqual(["cod-main"]);

    // Subagent view (its session id): only its own step.
    const sub = getEnrichmentForAgent(agentId, subSession);
    expect([...sub.keys()]).toEqual(["cod-sub"]);
  });

  it("replaces a scope's table wholesale without touching another scope", () => {
    const agentId = "agent-scope2";
    const subSession = "agent-sub2";
    applyEnrichmentSnapshot(agentId, { "cod-main": step("Main") });
    applyEnrichmentSnapshot(agentId, { "cod-sub-a": step("A") }, subSession);

    // A new subagent snapshot replaces only that subagent's table.
    applyEnrichmentSnapshot(agentId, { "cod-sub-b": step("B") }, subSession);

    expect([...getEnrichmentForAgent(agentId).keys()]).toEqual(["cod-main"]);
    expect([...getEnrichmentForAgent(agentId, subSession).keys()]).toEqual(["cod-sub-b"]);
  });
});
