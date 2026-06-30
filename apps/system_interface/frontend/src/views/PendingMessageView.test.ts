import { describe, expect, it, vi, beforeEach } from "vitest";
import type m from "mithril";
import type { PendingMessage } from "../models/PendingMessages";

// Mithril captures requestAnimationFrame at import time; polyfill before imports.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// renderUserMessage (pulled in transitively) imports the dockview/markdown
// module graph; stub those so this test exercises the real bubble renderer
// without the heavy DOM dependencies.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({ MarkdownContent: () => null }));

// The pending messages renderPendingMessages will see. Mutated per test. The
// mock exports every name PendingMessageView (and the Response module it pulls
// in) imports, so module linking succeeds.
let mockPending: PendingMessage[] = [];
vi.mock("../models/PendingMessages", () => ({
  getPendingMessages: () => mockPending,
  getPendingMessage: (_agentId: string, id: string) => mockPending.find((p) => p.id === id),
  markPendingMessageQueued: vi.fn(),
  markPendingMessageSending: vi.fn(),
  removePendingMessage: vi.fn(),
  reconcilePendingMessages: vi.fn(),
}));

// interruptAndResend drives the agent restart through these; mock them so the
// guard can be exercised without a backend.
vi.mock("../models/Response", () => ({
  interruptAgent: vi.fn(() => Promise.resolve()),
  sendMessage: vi.fn(() => Promise.resolve()),
}));

import { renderPendingMessages, interruptAndResend } from "./PendingMessageView";
import { interruptAgent, sendMessage } from "../models/Response";

function pending(id: string, status: "sending" | "queued"): PendingMessage {
  return { id, content: "hello there", status, sent_while_idle: true, prior_user_event_ids: new Set() };
}

// Mithril normalizes a vnode's `class` attr into `attrs.className`.
function vnodeClass(vnode: m.Vnode): string {
  return (vnode.attrs as { className?: string } | null)?.className ?? "";
}

function directChildClasses(wrapper: m.Vnode): string[] {
  return ((wrapper.children as m.Vnode[]) ?? []).map(vnodeClass);
}

/** Whether any vnode in the tree carries the given class. */
function hasClassInTree(node: unknown, cls: string): boolean {
  if (node === null || typeof node !== "object") return false;
  const vnode = node as m.Vnode;
  if (vnodeClass(vnode).split(/\s+/).includes(cls)) return true;
  const children = vnode.children;
  if (Array.isArray(children)) return children.some((c) => hasClassInTree(c, cls));
  return hasClassInTree(children, cls);
}

beforeEach(() => {
  mockPending = [];
  vi.clearAllMocks();
});

describe("renderPendingMessages", () => {
  it("renders a sending bubble dimmed, with a keyed Sending caption and no action", () => {
    mockPending = [pending("p1", "sending")];
    const nodes = renderPendingMessages("agent");

    expect(nodes).toHaveLength(1);
    const wrapper = nodes[0];
    expect(vnodeClass(wrapper)).toContain("pending-message--sending");
    expect(directChildClasses(wrapper)).toContain("pending-message-status");
    // No interrupt action while still sending.
    expect(hasClassInTree(wrapper, "pending-message-interrupt")).toBe(false);
    // The regression that crashed the panel: every wrapper child must be keyed.
    for (const child of (wrapper.children as m.Vnode[]) ?? []) {
      expect(child.key).toBeDefined();
    }
  });

  it("renders a queued bubble with an 'Interrupt and send' action", () => {
    mockPending = [pending("p1", "queued")];
    const nodes = renderPendingMessages("agent");

    expect(nodes).toHaveLength(1);
    const wrapper = nodes[0];
    expect(vnodeClass(wrapper)).toContain("pending-message--queued");
    expect(vnodeClass(wrapper)).not.toContain("--sending");
    expect(directChildClasses(wrapper)).toContain("pending-message-status");
    // The interrupt-and-send action is offered on a queued message.
    expect(hasClassInTree(wrapper, "pending-message-interrupt")).toBe(true);
    for (const child of (wrapper.children as m.Vnode[]) ?? []) {
      expect(child.key).toBeDefined();
    }
  });

  it("renders one wrapper per pending message and never throws on construction", () => {
    mockPending = [pending("p1", "sending"), pending("p2", "queued")];
    expect(() => renderPendingMessages("agent")).not.toThrow();
    expect(renderPendingMessages("agent")).toHaveLength(2);
  });

  it("returns nothing when there are no pending messages", () => {
    expect(renderPendingMessages("agent")).toHaveLength(0);
  });
});

describe("interruptAndResend", () => {
  // A controllable promise so the agent restart can be held mid-flight while a
  // second resend is attempted for the same agent.
  function deferred(): { promise: Promise<void>; resolve: () => void } {
    let resolve!: () => void;
    const promise = new Promise<void>((res) => {
      resolve = res;
    });
    return { promise, resolve };
  }

  it("ignores a second concurrent resend for the same agent while one is in flight", async () => {
    mockPending = [pending("p1", "queued"), pending("p2", "queued")];
    const gate = deferred();
    vi.mocked(interruptAgent).mockReturnValueOnce(gate.promise);

    // Start the first resend; its interrupt is now pending (gate unresolved).
    const first = interruptAndResend("agent", "p1");
    // A second bubble's resend for the same agent must be a no-op.
    await interruptAndResend("agent", "p2");

    expect(interruptAgent).toHaveBeenCalledTimes(1);
    expect(sendMessage).not.toHaveBeenCalled();

    // Let the first sequence complete.
    gate.resolve();
    await first;

    expect(interruptAgent).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("clears the guard so a later resend for the same agent runs", async () => {
    mockPending = [pending("p1", "queued")];
    await interruptAndResend("agent", "p1");
    expect(interruptAgent).toHaveBeenCalledTimes(1);

    // A separate, later resend (after the first settled) is not blocked.
    await interruptAndResend("agent", "p1");
    expect(interruptAgent).toHaveBeenCalledTimes(2);
    expect(sendMessage).toHaveBeenCalledTimes(2);
  });

  it("clears the guard on failure so a retry can run", async () => {
    mockPending = [pending("p1", "queued")];
    vi.mocked(interruptAgent).mockRejectedValueOnce(new Error("boom"));
    const alertMock = vi.fn();
    vi.stubGlobal("alert", alertMock);

    await interruptAndResend("agent", "p1");
    expect(interruptAgent).toHaveBeenCalledTimes(1);
    expect(alertMock).toHaveBeenCalledTimes(1);

    // The guard was released in the finally, so a retry proceeds.
    await interruptAndResend("agent", "p1");
    expect(interruptAgent).toHaveBeenCalledTimes(2);

    vi.unstubAllGlobals();
  });
});
