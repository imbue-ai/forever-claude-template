import { describe, expect, it, vi, beforeEach } from "vitest";
import type m from "mithril";
import type { PendingMessage } from "../models/PendingMessages";

// renderUserMessage (pulled in transitively) imports the dockview/markdown
// module graph; stub those so this test exercises the real bubble renderer
// without the heavy DOM dependencies.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({ MarkdownContent: () => null }));

// The pending messages renderPendingMessages will see. Mutated per test.
let mockPending: PendingMessage[] = [];
vi.mock("../models/PendingMessages", () => ({
  getPendingMessages: () => mockPending,
}));

import { renderPendingMessages } from "./PendingMessageView";

function pending(id: string, status: "sending" | "delivered"): PendingMessage {
  return { id, content: "hello there", status, sent_while_idle: true, prior_user_event_ids: new Set() };
}

// Mithril normalizes a vnode's `class` attr into `attrs.className` (leaving
// `attrs.class` null), so read className when inspecting rendered vnodes.
function vnodeClass(vnode: m.Vnode): string {
  return (vnode.attrs as { className?: string } | null)?.className ?? "";
}

function childClasses(wrapper: m.Vnode): string[] {
  const children = (wrapper.children as m.Vnode[]) ?? [];
  return children.map(vnodeClass);
}

beforeEach(() => {
  mockPending = [];
});

describe("renderPendingMessages", () => {
  it("renders a sending bubble dimmed, with a keyed Sending caption", () => {
    mockPending = [pending("p1", "sending")];
    const nodes = renderPendingMessages("agent");

    expect(nodes).toHaveLength(1);
    const wrapper = nodes[0];
    expect(vnodeClass(wrapper)).toContain("pending-message--sending");
    // The status caption is present...
    expect(childClasses(wrapper)).toContain("pending-message-status");
    // ...and -- the regression that crashed the panel -- every child of the
    // wrapper is keyed (renderUserMessage's bubble is keyed, so its siblings
    // must be too, or Mithril throws when the vnode is constructed).
    for (const child of (wrapper.children as m.Vnode[]) ?? []) {
      expect(child.key).toBeDefined();
    }
  });

  it("renders a delivered bubble with no sending affordance", () => {
    mockPending = [pending("p1", "delivered")];
    const nodes = renderPendingMessages("agent");

    expect(nodes).toHaveLength(1);
    const wrapper = nodes[0];
    expect(vnodeClass(wrapper)).toBe("pending-message");
    expect(vnodeClass(wrapper)).not.toContain("--sending");
    expect(childClasses(wrapper)).not.toContain("pending-message-status");
  });

  it("renders one wrapper per pending message and never throws on construction", () => {
    mockPending = [pending("p1", "sending"), pending("p2", "delivered")];
    expect(() => renderPendingMessages("agent")).not.toThrow();
    expect(renderPendingMessages("agent")).toHaveLength(2);
  });

  it("returns nothing when there are no pending messages", () => {
    expect(renderPendingMessages("agent")).toHaveLength(0);
  });
});
