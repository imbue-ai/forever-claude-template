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

import { renderPendingMessages } from "./PendingMessageView";

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
