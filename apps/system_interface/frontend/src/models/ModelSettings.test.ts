import { beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend responses and asserts
// the POST order without a real network call. redraw is a no-op; apiUrl is
// identity so URLs are predictable.
const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import { fetchModelSettings, getModelSettings, setFastMode } from "./ModelSettings";

interface RequestOptions {
  method: string;
  url: string;
  body?: { enabled?: boolean; model?: string };
}

const OPTIONS = [
  { id: "opus[1m]", label: "Opus 4.8", supports_fast_mode: true },
  { id: "sonnet", label: "Sonnet 5", supports_fast_mode: false },
];

function makeSettings(model: string, fastMode: boolean) {
  return { model, fast_mode: fastMode, fast_mode_supported: model.startsWith("opus"), options: OPTIONS };
}

// Let the pending promise callbacks (POST resolution -> settle read -> redraw) run.
async function flush(): Promise<void> {
  for (let i = 0; i < 4; i++) {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
}

beforeEach(() => {
  mockRequest.mockReset();
});

describe("ModelSettings apply chain", () => {
  it("applies rapid fast-mode toggles one at a time, in click order", async () => {
    const agentId = "agent-order";
    const postedEnabled: boolean[] = [];
    // Gate the first POST so we can observe that the second waits behind it
    // rather than racing it (the bug: concurrent, unordered delivery).
    let releaseFirstPost: () => void = () => {};
    const firstPostGate = new Promise<void>((resolve) => {
      releaseFirstPost = resolve;
    });
    let postCount = 0;
    mockRequest.mockImplementation((options: RequestOptions) => {
      if (options.method === "GET") {
        return Promise.resolve(makeSettings("opus[1m]", false));
      }
      postCount += 1;
      postedEnabled.push(options.body!.enabled!);
      return postCount === 1 ? firstPostGate : Promise.resolve();
    });

    await fetchModelSettings(agentId);
    setFastMode(agentId, true);
    setFastMode(agentId, false);
    await flush();

    // Only the first change has been sent; the second is queued behind it.
    expect(postedEnabled).toEqual([true]);

    releaseFirstPost();
    await flush();

    // Both applied, in the order the user clicked.
    expect(postedEnabled).toEqual([true, false]);
  });

  it("reflects a pick optimistically, then reconciles to the agent's real state", async () => {
    const agentId = "agent-reconcile";
    // The agent refuses the change: the POST succeeds at the HTTP level but the
    // settle read still reports fast mode off.
    mockRequest.mockImplementation((options: RequestOptions) => {
      if (options.method === "GET") {
        return Promise.resolve(makeSettings("opus[1m]", false));
      }
      return Promise.resolve();
    });

    await fetchModelSettings(agentId);
    setFastMode(agentId, true);

    // Optimistic: the toggle shows on immediately, before the settle read.
    expect(getModelSettings(agentId)?.fast_mode).toBe(true);

    await flush();

    // The settle read wins: the picker shows the agent's real (refused) state.
    expect(getModelSettings(agentId)?.fast_mode).toBe(false);
  });
});
