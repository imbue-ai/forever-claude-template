import { describe, expect, it, vi } from "vitest";

// message-renderers pulls in dockview-core (via DockviewWorkspace), marked and
// DOMPurify (via markdown). None are needed to exercise the pure
// buildToolResultsMap memoization, so stub them out to keep this a fast,
// DOM-free unit test.
vi.mock("mithril", () => ({ default: {} }));
vi.mock("../markdown", () => ({ MarkdownContent: {} }));
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: () => {} }));

import { buildToolResultsMap } from "./message-renderers";
import type { TranscriptEvent } from "../models/Response";

function toolResult(id: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "tool_result",
    event_id: `evt-${id}`,
    source: "test",
    message_uuid: `uuid-${id}`,
    tool_call_id: id,
    output: `output-${id}`,
  };
}

function userMessage(id: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: `evt-${id}`,
    source: "test",
    message_uuid: `uuid-${id}`,
    content: "hi",
  };
}

describe("buildToolResultsMap", () => {
  it("indexes tool_result events by tool_call_id and ignores non-tool-results", () => {
    const events = [userMessage("u1"), toolResult("call-1"), toolResult("call-2")];
    const map = buildToolResultsMap(events);
    expect([...map.keys()].sort()).toEqual(["call-1", "call-2"]);
    expect(map.get("call-1")?.output).toBe("output-call-1");
  });

  it("returns the same cached Map for the same events array identity", () => {
    const events = [toolResult("call-1")];
    const first = buildToolResultsMap(events);
    const second = buildToolResultsMap(events);
    // Same array reference -> memoized, not rebuilt.
    expect(second).toBe(first);
  });

  it("rebuilds when the events array identity changes", () => {
    const eventsA = [toolResult("call-1")];
    const eventsB = [toolResult("call-1")];
    const mapA = buildToolResultsMap(eventsA);
    const mapB = buildToolResultsMap(eventsB);
    // Different array reference (as produced by appendEvents/prependEvents
    // replacing the array) -> a freshly built map.
    expect(mapB).not.toBe(mapA);
  });
});
