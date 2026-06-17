import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request/redraw via hoisted mocks so the test can control
// when the snapshot fetch resolves without fighting mithril's overloaded types.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({
  mockRequest: vi.fn(),
  mockRedraw: vi.fn(),
}));
vi.mock("mithril", () => ({
  default: { request: mockRequest, redraw: mockRedraw },
}));

import {
  connectToStream,
  disconnectFromStream,
  getStreamingPreview,
  loadSnapshotWithStream,
  previewHasNewContent,
  shouldShowStreamingPreview,
} from "./StreamingMessage";
import { getEventsForAgent, type TranscriptEvent } from "./Response";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
}

function makeEvent(id: string, content: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    role: "user",
    content,
  };
}

let agentCounter = 0;

beforeEach(() => {
  FakeEventSource.instances = [];
  globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
  globalThis.document = { querySelector: () => null } as unknown as Document;
  mockRequest.mockReset();
  mockRedraw.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("loadSnapshotWithStream", () => {
  it("does not drop an SSE delta that races the initial snapshot fetch", async () => {
    const agentId = `agent-${agentCounter++}`;
    const snapshotEvent = makeEvent("snap-1", "from snapshot");
    const delta = makeEvent("delta-1", "live delta during fetch");

    // Leave the snapshot fetch pending so we can interleave a live delta.
    const snapshotRequest = deferred<{ events: TranscriptEvent[] }>();
    mockRequest.mockReturnValue(snapshotRequest.promise);

    const loadPromise = loadSnapshotWithStream(agentId);

    // The stream is open and the snapshot is still in flight: a live event
    // arrives now. Without buffering, the snapshot replace below would drop it.
    const eventSource = FakeEventSource.instances[FakeEventSource.instances.length - 1];
    expect(eventSource).toBeDefined();
    eventSource?.onmessage?.({ data: JSON.stringify(delta) });

    snapshotRequest.resolve({ events: [snapshotEvent] });
    await loadPromise;

    const ids = getEventsForAgent(agentId).map((event) => event.event_id);
    expect(ids).toContain("snap-1");
    expect(ids).toContain("delta-1");
  });
});

function makeAssistantEvent(id: string, text: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "assistant_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    model: "test-model",
    text,
    tool_calls: [],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

function lastEventSource(): FakeEventSource {
  const source = FakeEventSource.instances[FakeEventSource.instances.length - 1];
  expect(source).toBeDefined();
  return source as FakeEventSource;
}

describe("assistant_streaming preview", () => {
  it("sets and updates the preview from streaming frames", () => {
    const agentId = `agent-${agentCounter++}`;
    connectToStream(agentId);
    const source = lastEventSource();

    source.onmessage?.({
      data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "p", text: "Thinking" }),
    });
    expect(getStreamingPreview(agentId)).toBe("Thinking");

    source.onmessage?.({
      data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "p", text: "Thinking harder" }),
    });
    expect(getStreamingPreview(agentId)).toBe("Thinking harder");

    disconnectFromStream(agentId);
  });

  it("clears the preview on an empty streaming frame (agent went idle)", () => {
    const agentId = `agent-${agentCounter++}`;
    connectToStream(agentId);
    const source = lastEventSource();

    source.onmessage?.({
      data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "p", text: "partial" }),
    });
    expect(getStreamingPreview(agentId)).toBe("partial");

    source.onmessage?.({ data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "q", text: "" }) });
    expect(getStreamingPreview(agentId)).toBeNull();

    disconnectFromStream(agentId);
  });

  it("clears the preview when the finalized assistant_message lands", () => {
    const agentId = `agent-${agentCounter++}`;
    connectToStream(agentId);
    const source = lastEventSource();

    source.onmessage?.({
      data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "p", text: "almost done" }),
    });
    expect(getStreamingPreview(agentId)).toBe("almost done");

    // The durable transcript event arrives and supersedes the live preview.
    source.onmessage?.({ data: JSON.stringify(makeAssistantEvent("final-1", "almost done, now complete")) });
    expect(getStreamingPreview(agentId)).toBeNull();
    expect(getEventsForAgent(agentId).map((e) => e.event_id)).toContain("final-1");

    disconnectFromStream(agentId);
  });

  it("clears the preview when a new user message starts the next turn", () => {
    const agentId = `agent-${agentCounter++}`;
    connectToStream(agentId);
    const source = lastEventSource();

    source.onmessage?.({
      data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "p", text: "prior turn output" }),
    });
    expect(getStreamingPreview(agentId)).toBe("prior turn output");

    // The user sends another message: the prior turn's in-progress text must not
    // linger into the new turn.
    source.onmessage?.({ data: JSON.stringify(makeEvent("user-2", "do the next thing")) });
    expect(getStreamingPreview(agentId)).toBeNull();

    disconnectFromStream(agentId);
  });

  it("keeps the preview when a non-boundary user_message arrives mid-turn", () => {
    const agentId = `agent-${agentCounter++}`;
    connectToStream(agentId);
    const source = lastEventSource();

    source.onmessage?.({
      data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "p", text: "still working" }),
    });
    expect(getStreamingPreview(agentId)).toBe("still working");

    // A skill expansion is emitted as a user_message while the same logical turn
    // is still in flight; it must not clear the live preview (which would flicker
    // the bubble off until the next snapshot frame).
    source.onmessage?.({
      data: JSON.stringify(makeEvent("skill-1", "Base directory for this skill: /x/skills/foo/bar")),
    });
    expect(getStreamingPreview(agentId)).toBe("still working");

    // Stop-hook feedback is likewise non-boundary mid-turn chrome.
    source.onmessage?.({ data: JSON.stringify(makeEvent("hook-1", "Stop hook feedback:\nkeep going")) });
    expect(getStreamingPreview(agentId)).toBe("still working");

    disconnectFromStream(agentId);
  });

  it("drops the preview when the stream is intentionally disconnected", () => {
    const agentId = `agent-${agentCounter++}`;
    connectToStream(agentId);
    const source = lastEventSource();

    source.onmessage?.({
      data: JSON.stringify({ type: "assistant_streaming", last_complete_id: "p", text: "in progress" }),
    });
    expect(getStreamingPreview(agentId)).toBe("in progress");

    disconnectFromStream(agentId);
    expect(getStreamingPreview(agentId)).toBeNull();
  });
});

describe("shouldShowStreamingPreview", () => {
  const base = {
    previewText: "the agent is typing this",
    latestAssistantText: "an earlier, finalized message",
    activityState: "THINKING" as string | null | undefined,
    hasMoreAfter: false,
  };

  it("shows while genuinely streaming new text", () => {
    expect(shouldShowStreamingPreview(base)).toBe(true);
  });

  it("hides when there is no preview text", () => {
    expect(shouldShowStreamingPreview({ ...base, previewText: null })).toBe(false);
    expect(shouldShowStreamingPreview({ ...base, previewText: "" })).toBe(false);
  });

  it("hides when scrolled off the live tail", () => {
    expect(shouldShowStreamingPreview({ ...base, hasMoreAfter: true })).toBe(false);
  });

  it("hides when the agent is idle (no response in flight)", () => {
    expect(shouldShowStreamingPreview({ ...base, activityState: "IDLE" })).toBe(false);
  });

  it("hides when the preview already equals the latest finalized message", () => {
    // The core bug: mngr keeps the just-finalized message as the buffer body, so
    // the preview would otherwise double the rendered turn / re-appear next turn.
    const finalized = "Here is my complete answer.";
    expect(
      shouldShowStreamingPreview({
        previewText: finalized,
        latestAssistantText: finalized,
        activityState: "THINKING",
        hasMoreAfter: false,
      }),
    ).toBe(false);
  });

  it("treats cosmetic whitespace differences as a match (still hidden)", () => {
    expect(
      shouldShowStreamingPreview({
        previewText: "Line one  \nLine two\n\n",
        latestAssistantText: "Line one\nLine two",
        activityState: "THINKING",
        hasMoreAfter: false,
      }),
    ).toBe(false);
  });

  it("shows a genuinely new message that differs from the last finalized one", () => {
    expect(
      shouldShowStreamingPreview({
        previewText: "A brand new response, mid-stream",
        latestAssistantText: "The previous, finished response",
        activityState: "THINKING",
        hasMoreAfter: false,
      }),
    ).toBe(true);
  });

  it("still shows a follow-up message that extends the finalized one with new prose", () => {
    // The finalized message lingers in the buffer, then the next turn streams in
    // below it: the preview has genuinely new content past the finalized text.
    expect(
      shouldShowStreamingPreview({
        previewText: "First answer.\n\nFirst answer.\n\nNow the follow-up is being typed",
        latestAssistantText: "First answer.",
        activityState: "THINKING",
        hasMoreAfter: false,
      }),
    ).toBe(true);
  });
});

describe("previewHasNewContent", () => {
  it("is false when the preview only re-renders the finalized message", () => {
    expect(previewHasNewContent("Here is the answer.", "Here is the answer.")).toBe(false);
  });

  it("tolerates cosmetic whitespace/reflow differences in the rendering", () => {
    // mngr's reverse-mapped pane differs from the transcript by trailing spaces
    // and a collapsed blank line, but carries no new content.
    expect(previewHasNewContent("Para one.  \n\n\nPara two.\n", "Para one.\n\nPara two.")).toBe(false);
  });

  it("is true when the preview adds non-whitespace content beyond the finalized message", () => {
    expect(previewHasNewContent("Para one.\n\nPara two, still streaming", "Para one.")).toBe(true);
  });

  it("is true for a wholly different in-progress message", () => {
    expect(previewHasNewContent("A completely different response", "The old finalized one")).toBe(true);
  });
});
