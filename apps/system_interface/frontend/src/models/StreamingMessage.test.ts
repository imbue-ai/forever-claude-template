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

import { loadSnapshotWithStream } from "./StreamingMessage";
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
