import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request via a hoisted mock so the test can assert the POST
// body without a real network call. apiUrl is stubbed to identity so the URL is
// predictable.
const { mockRequest } = vi.hoisted(() => ({
  mockRequest: vi.fn(() => Promise.resolve()),
}));
vi.mock("mithril", () => ({
  default: { request: mockRequest },
}));
vi.mock("../base-path", () => ({
  apiUrl: (path: string) => path,
}));

import { reportActivity, reportMessaged } from "./activityReporter";

interface ActivityBody {
  open: string[];
  visible: string[];
  messaged: string | null;
}

function lastBody(): ActivityBody {
  const calls = mockRequest.mock.calls as unknown as Array<[{ body: ActivityBody }]>;
  expect(calls.length).toBeGreaterThan(0);
  return calls[calls.length - 1][0].body;
}

beforeEach(() => {
  vi.useFakeTimers();
  mockRequest.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("activityReporter", () => {
  it("debounces a burst of reports into a single POST with the latest presence", () => {
    reportActivity({ open: ["a"], visible: ["a"] });
    reportActivity({ open: ["a", "b"], visible: ["b"] });
    reportActivity({ open: ["a", "b", "c"], visible: ["c"] });
    // Nothing sent until the debounce elapses.
    expect(mockRequest).not.toHaveBeenCalled();

    vi.advanceTimersByTime(250);

    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(lastBody()).toEqual({ open: ["a", "b", "c"], visible: ["c"], messaged: null });
  });

  it("sends messaged: null for a presence-only report", () => {
    reportActivity({ open: ["a"], visible: ["a"] });
    vi.advanceTimersByTime(250);
    expect(lastBody().messaged).toBeNull();
  });

  it("carries a recency bump alongside the retained presence", () => {
    // A presence report establishes the current tabs...
    reportActivity({ open: ["a", "b"], visible: ["a"] });
    vi.advanceTimersByTime(250);
    mockRequest.mockClear();

    // ...then a message to ``a`` is reported without re-sending presence; the
    // retained open/visible sets ride along with the recency bump.
    reportMessaged("a");
    vi.advanceTimersByTime(250);

    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(lastBody()).toEqual({ open: ["a", "b"], visible: ["a"], messaged: "a" });
  });

  it("clears the messaged flag after it is sent so the next report is presence-only", () => {
    reportActivity({ open: ["a"], visible: ["a"] });
    reportMessaged("a");
    vi.advanceTimersByTime(250);
    expect(lastBody().messaged).toBe("a");

    reportActivity({ open: ["a"], visible: ["a"] });
    vi.advanceTimersByTime(250);
    expect(lastBody().messaged).toBeNull();
  });
});
