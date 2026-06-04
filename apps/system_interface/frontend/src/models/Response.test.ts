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
  getEventsForAgent,
  type AssistantMessageEvent,
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
