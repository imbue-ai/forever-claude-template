import { afterEach, describe, expect, it, vi } from "vitest";

import { buildSessionTerminalUrl, initAgentManager } from "./AgentManager";

/** Read back the repeated ``arg`` query params in order. */
function parseArgs(url: string): string[] {
  const query = url.split("?")[1] ?? "";
  return new URLSearchParams(query).getAll("arg");
}

describe("buildSessionTerminalUrl", () => {
  it("emits the positional args in ttyd dispatch order", () => {
    const url = buildSessionTerminalUrl("terminal-1", "term-abc", "/mngr/code");
    expect(url.startsWith("/service/terminal/?")).toBe(true);
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-1", "term-abc", "/mngr/code"]);
  });

  it("omits the working directory arg as empty when none is given", () => {
    const url = buildSessionTerminalUrl("terminal-2", "term-xyz", "");
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-2", "term-xyz", ""]);
  });

  it("percent-encodes special characters but round-trips the original values", () => {
    const url = buildSessionTerminalUrl("my term", "id", "/a b/c");
    // The raw query must not carry literal spaces...
    expect(url).not.toContain(" ");
    // ...but decoding recovers the exact session name and workdir.
    expect(parseArgs(url)).toEqual(["_", "session", "my term", "id", "/a b/c"]);
  });
});

// A fake WebSocket that records construction and, like a real browser, fires
// ``onclose`` from ``close()`` -- so the test proves the wake path detaches the
// dead socket's handlers before closing it.
class FakeWebSocket {
  static readonly instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
    this.onclose?.();
  }
}

describe("wake reconnect", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWebSocket.instances.length = 0;
  });

  it("tears down the stale socket and opens a fresh one when the machine wakes", () => {
    const listeners = new Map<string, (() => void)[]>();
    const capture = (type: string, cb: () => void): void => {
      listeners.set(type, [...(listeners.get(type) ?? []), cb]);
    };
    const fire = (type: string): void => {
      for (const cb of listeners.get(type) ?? []) {
        cb();
      }
    };

    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("document", { addEventListener: capture, querySelector: () => null, visibilityState: "visible" });
    vi.stubGlobal("window", { addEventListener: capture, location: { protocol: "http:", host: "localhost:8000" } });

    initAgentManager();
    expect(FakeWebSocket.instances).toHaveLength(1);
    const stale = FakeWebSocket.instances[0];

    // The machine wakes: the browser never fired onclose on the dead socket, so
    // becoming visible is what has to trigger the reconnect.
    fire("visibilitychange");

    // The stale socket was closed and a brand-new one opened.
    expect(stale.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
    // Its handlers were detached first, so the late onclose from close() could
    // not run against (and null out) the freshly opened replacement.
    expect(stale.onclose).toBeNull();
    expect(FakeWebSocket.instances[1].onclose).not.toBeNull();

    // The burst of focus + online that a single wake also fires is coalesced
    // into the one reconnect above, not one reconnect per event.
    fire("focus");
    fire("online");
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});
