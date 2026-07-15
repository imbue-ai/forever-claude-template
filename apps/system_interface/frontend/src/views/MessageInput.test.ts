import { describe, expect, it, vi, beforeEach } from "vitest";

// Mithril captures requestAnimationFrame at import time to schedule redraws;
// the default (node) test env has none, so the m.redraw() calls inside the
// send handler would throw. Polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// Mutable test doubles shared between the mock factories (which are hoisted
// above the imports) and the tests. The connection-listener registry lets a
// test drive the reconnect edge that the send handler subscribes to.
const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  addPendingMessage: vi.fn(),
  getPendingMessage: vi.fn(),
  markPendingMessageQueued: vi.fn(),
  markPendingMessageReconnecting: vi.fn(),
  markPendingMessageSending: vi.fn(),
  removePendingMessage: vi.fn(),
  connectionListeners: [] as Array<(connected: boolean) => void>,
}));

vi.mock("../models/ComposerAttachments", () => ({
  clearComposerAttachments: vi.fn(),
  getComposerAttachments: () => [],
  getReadyAttachmentPaths: () => [],
  hasReadyAttachments: () => false,
  removeComposerAttachment: vi.fn(),
  restoreComposerAttachments: vi.fn(),
  uploadFilesToComposer: vi.fn(),
  waitForComposerUploads: () => Promise.resolve(),
}));

vi.mock("../models/attachments", () => ({
  buildMessageWithAttachments: (text: string) => text,
  formatFileSize: () => "",
}));

vi.mock("../models/Response", () => ({
  sendMessage: h.sendMessage,
  interruptAgent: vi.fn(() => Promise.resolve()),
  getEventsForAgent: () => [],
}));

vi.mock("../models/PendingMessages", () => ({
  addPendingMessage: h.addPendingMessage,
  getEffectiveActivityState: () => "IDLE",
  getPendingMessage: h.getPendingMessage,
  markPendingMessageQueued: h.markPendingMessageQueued,
  markPendingMessageReconnecting: h.markPendingMessageReconnecting,
  markPendingMessageSending: h.markPendingMessageSending,
  removePendingMessage: h.removePendingMessage,
}));

vi.mock("../models/AgentManager", () => ({
  addConnectionStateListener: (listener: (connected: boolean) => void) => {
    h.connectionListeners.push(listener);
  },
  removeConnectionStateListener: (listener: (connected: boolean) => void) => {
    const index = h.connectionListeners.indexOf(listener);
    if (index >= 0) {
      h.connectionListeners.splice(index, 1);
    }
  },
}));

vi.mock("./ActivityIndicator", () => ({ isWorkingActivityState: () => false }));

// The real request-error module is used unmocked so the send handler's
// unreachable-vs-application classification is exercised end to end.
import { MessageInput } from "./MessageInput";

const AGENT = "agent-1";
const PENDING_ID = "pending-1";

let alertMock: ReturnType<typeof vi.fn>;

type VnodeLike = { attrs?: Record<string, unknown>; children?: unknown };

function* walk(node: unknown): Generator<VnodeLike> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (node !== null && typeof node === "object") {
    const vnode = node as VnodeLike;
    yield vnode;
    if (vnode.children !== undefined) yield* walk(vnode.children);
  }
}

function findByClass(tree: unknown, className: string): VnodeLike | undefined {
  for (const vnode of walk(tree)) {
    const classes = vnode.attrs?.className;
    if (typeof classes === "string" && classes.split(/\s+/).includes(className)) {
      return vnode;
    }
  }
  return undefined;
}

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function makeInput(): { render: () => unknown } {
  const component = MessageInput();
  const vnode = { attrs: { agentId: AGENT } };
  return { render: () => component.view(vnode as Parameters<typeof component.view>[0]) };
}

// Type text into the composer and click send, driving the component exactly as
// a user would: set the textarea value via its oninput, then invoke the send
// button's onclick (which is handleSend). Resolves once the async send settles.
async function typeAndSend(input: { render: () => unknown }, text: string): Promise<void> {
  const textarea = findByClass(input.render(), "message-input-textbox");
  const oninput = textarea?.attrs?.oninput as (event: unknown) => void;
  oninput({ target: { value: text, style: {}, scrollHeight: 20 } });
  const sendButton = findByClass(input.render(), "message-input-send-button");
  const onclick = sendButton?.attrs?.onclick as () => void;
  onclick();
  await flush();
}

beforeEach(() => {
  vi.clearAllMocks();
  h.connectionListeners = [];
  h.addPendingMessage.mockReturnValue(PENDING_ID);
  // The pending message is still held (never rolled back) throughout the
  // reconnecting flow, so the retry's existence check always finds it.
  h.getPendingMessage.mockReturnValue({ id: PENDING_ID, status: "reconnecting" });
  alertMock = vi.fn();
  globalThis.alert = alertMock as unknown as typeof globalThis.alert;
  const store = new Map<string, string>();
  globalThis.localStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  } as Storage;
});

describe("MessageInput send failure handling", () => {
  it("holds the message as reconnecting on a backend-unreachable failure instead of alerting", async () => {
    h.sendMessage.mockRejectedValueOnce({ code: 503 });
    const input = makeInput();

    await typeAndSend(input, "hello there");

    expect(h.markPendingMessageReconnecting).toHaveBeenCalledWith(AGENT, PENDING_ID);
    expect(h.removePendingMessage).not.toHaveBeenCalled();
    expect(alertMock).not.toHaveBeenCalled();
    // A retry listener is subscribed to await the reconnect.
    expect(h.connectionListeners).toHaveLength(1);
  });

  it("resends and marks the held message queued when the connection recovers", async () => {
    h.sendMessage.mockRejectedValueOnce({ code: 503 });
    const input = makeInput();
    await typeAndSend(input, "hello there");
    expect(h.connectionListeners).toHaveLength(1);

    // The connection comes back and the resend succeeds.
    h.sendMessage.mockResolvedValueOnce(undefined);
    h.connectionListeners[0](true);
    await flush();

    expect(h.markPendingMessageSending).toHaveBeenCalledWith(AGENT, PENDING_ID);
    expect(h.markPendingMessageQueued).toHaveBeenCalledWith(AGENT, PENDING_ID);
    // The listener is torn down once the message is delivered.
    expect(h.connectionListeners).toHaveLength(0);
    expect(alertMock).not.toHaveBeenCalled();
  });

  it("keeps waiting when a resend hits another connectivity error", async () => {
    h.sendMessage.mockRejectedValueOnce({ code: 503 });
    const input = makeInput();
    await typeAndSend(input, "hello there");

    // First reconnect edge: the backend is still unreachable.
    h.sendMessage.mockRejectedValueOnce({ code: 502 });
    h.connectionListeners[0](true);
    await flush();

    // The message returns to reconnecting (called once on first failure, once on
    // the failed retry) and the listener stays subscribed for the next edge.
    expect(h.markPendingMessageReconnecting).toHaveBeenCalledTimes(2);
    expect(h.connectionListeners).toHaveLength(1);
    expect(h.removePendingMessage).not.toHaveBeenCalled();
    expect(alertMock).not.toHaveBeenCalled();

    // Second reconnect edge: the backend is back and the resend succeeds.
    h.sendMessage.mockResolvedValueOnce(undefined);
    h.connectionListeners[0](true);
    await flush();

    expect(h.markPendingMessageQueued).toHaveBeenCalledWith(AGENT, PENDING_ID);
    expect(h.connectionListeners).toHaveLength(0);
  });

  it("rolls back and alerts when a resend fails with a real application error", async () => {
    h.sendMessage.mockRejectedValueOnce({ code: 503 });
    const input = makeInput();
    await typeAndSend(input, "hello there");

    // The connection recovers but the backend rejects the message for real.
    h.sendMessage.mockRejectedValueOnce({ response: { detail: "message too long" } });
    h.connectionListeners[0](true);
    await flush();

    expect(h.removePendingMessage).toHaveBeenCalledWith(AGENT, PENDING_ID);
    expect(alertMock).toHaveBeenCalledWith("Failed to send message: message too long");
    expect(h.connectionListeners).toHaveLength(0);
  });

  it("stops retrying once the give-up backstop has dropped the held message", async () => {
    h.sendMessage.mockRejectedValueOnce({ code: 503 });
    const input = makeInput();
    await typeAndSend(input, "hello there");

    // The backstop expired the message while the connection was down.
    h.getPendingMessage.mockReturnValue(undefined);
    h.sendMessage.mockClear();
    h.connectionListeners[0](true);
    await flush();

    // No resend is attempted, and the now-orphaned listener unsubscribes.
    expect(h.sendMessage).not.toHaveBeenCalled();
    expect(h.connectionListeners).toHaveLength(0);
  });

  it("rolls back and alerts on a genuine application error without holding or retrying", async () => {
    h.sendMessage.mockRejectedValueOnce({ response: { detail: "no such agent" } });
    const input = makeInput();

    await typeAndSend(input, "hello there");

    expect(h.removePendingMessage).toHaveBeenCalledWith(AGENT, PENDING_ID);
    expect(alertMock).toHaveBeenCalledWith("Failed to send message: no such agent");
    expect(h.markPendingMessageReconnecting).not.toHaveBeenCalled();
    expect(h.connectionListeners).toHaveLength(0);
  });

  it("marks the message queued on a successful send", async () => {
    h.sendMessage.mockResolvedValueOnce(undefined);
    const input = makeInput();

    await typeAndSend(input, "hello there");

    expect(h.markPendingMessageQueued).toHaveBeenCalledWith(AGENT, PENDING_ID);
    expect(h.markPendingMessageReconnecting).not.toHaveBeenCalled();
    expect(alertMock).not.toHaveBeenCalled();
    expect(h.connectionListeners).toHaveLength(0);
  });
});
