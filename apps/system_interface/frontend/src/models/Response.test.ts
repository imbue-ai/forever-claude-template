import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril is mocked: request is driven per-test, redraw is a no-op spy. This
// keeps the store unit-testable without a DOM or a real network.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({
  mockRequest: vi.fn(),
  mockRedraw: vi.fn(),
}));
vi.mock("mithril", () => ({
  default: { request: mockRequest, redraw: mockRedraw },
}));

import {
  appendEvents,
  prependEvents,
  evictOldEvents,
  fetchEvents,
  fetchBackfillEvents,
  getEventsForAgent,
  getEventCount,
  getFirstEventId,
  hasMoreToBackfill,
  isBackfillComplete,
  MAX_HELD_EVENTS,
  EVICT_TARGET_EVENTS,
  type TranscriptEvent,
} from "./Response";

function makeEvent(id: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    content: id,
  };
}

let counter = 0;
function freshAgent(): string {
  return `agent-${counter++}`;
}

beforeEach(() => {
  // base-path reads a <meta> tag via document.querySelector when building URLs.
  globalThis.document = { querySelector: () => null } as unknown as Document;
  mockRequest.mockReset();
  mockRedraw.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function ids(agentId: string): string[] {
  return getEventsForAgent(agentId).map((e) => e.event_id);
}

describe("dedup", () => {
  it("appendEvents ignores ids already present", () => {
    const agent = freshAgent();
    appendEvents(agent, [makeEvent("a"), makeEvent("b")]);
    appendEvents(agent, [makeEvent("b"), makeEvent("c")]);
    expect(ids(agent)).toEqual(["a", "b", "c"]);
  });

  it("prependEvents ignores ids already present and keeps order", () => {
    const agent = freshAgent();
    appendEvents(agent, [makeEvent("c"), makeEvent("d")]);
    prependEvents(agent, [makeEvent("a"), makeEvent("b"), makeEvent("c")]);
    expect(ids(agent)).toEqual(["a", "b", "c", "d"]);
  });
});

describe("has_more", () => {
  it("fetchEvents records the server has_more flag", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x")], has_more: true });
    await fetchEvents(agent);
    expect(hasMoreToBackfill(agent)).toBe(true);
    expect(isBackfillComplete(agent)).toBe(false);
  });

  it("treats a response without has_more as no more history", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("x")] });
    await fetchEvents(agent);
    expect(hasMoreToBackfill(agent)).toBe(false);
  });

  it("backfill is a no-op once the server reports no more history", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("b"), makeEvent("c")], has_more: true });
    await fetchEvents(agent);

    // First backfill page returns the remaining history with has_more=false.
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("a")], has_more: false });
    await fetchBackfillEvents(agent);
    expect(ids(agent)).toEqual(["a", "b", "c"]);
    expect(hasMoreToBackfill(agent)).toBe(false);

    // A subsequent backfill must not hit the network at all.
    mockRequest.mockClear();
    await fetchBackfillEvents(agent);
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("backfill pages before the first held event", async () => {
    const agent = freshAgent();
    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e5")], has_more: true });
    await fetchEvents(agent);

    mockRequest.mockResolvedValueOnce({ events: [makeEvent("e3"), makeEvent("e4")], has_more: true });
    await fetchBackfillEvents(agent);

    const call = mockRequest.mock.calls[mockRequest.mock.calls.length - 1][0];
    expect(call.params.before).toBe("e5");
    expect(ids(agent)).toEqual(["e3", "e4", "e5"]);
  });
});

describe("evictOldEvents", () => {
  it("does nothing below the cap", () => {
    const agent = freshAgent();
    appendEvents(
      agent,
      Array.from({ length: 10 }, (_v, i) => makeEvent(`e${i}`)),
    );
    expect(evictOldEvents(agent)).toBe(0);
    expect(getEventCount(agent)).toBe(10);
  });

  it("trims the oldest down to the target and flags more history", () => {
    const agent = freshAgent();
    const events = Array.from({ length: MAX_HELD_EVENTS + 200 }, (_v, i) => makeEvent(`e${i}`));
    appendEvents(agent, events);

    const removed = evictOldEvents(agent);
    expect(removed).toBe(events.length - EVICT_TARGET_EVENTS);
    expect(getEventCount(agent)).toBe(EVICT_TARGET_EVENTS);
    // The oldest are gone; the newest are kept.
    expect(getFirstEventId(agent)).toBe(`e${removed}`);
    // Evicted history is still on the server, so backfill is re-enabled.
    expect(hasMoreToBackfill(agent)).toBe(true);
  });

  it("re-admits evicted ids on a later prepend (dedup set was pruned)", () => {
    const agent = freshAgent();
    const events = Array.from({ length: MAX_HELD_EVENTS + 50 }, (_v, i) => makeEvent(`e${i}`));
    appendEvents(agent, events);
    const removed = evictOldEvents(agent);
    // Re-fetching an evicted event prepends it again rather than being deduped away.
    const reFetched = makeEvent("e0");
    prependEvents(agent, [reFetched]);
    expect(getFirstEventId(agent)).toBe("e0");
    expect(removed).toBeGreaterThan(0);
  });
});
