import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import { renderUserMessage } from "./message-renderers";

// Avoid importing the heavy/DOM-dependent module graph (dockview, dompurify) at
// test time; renderUserMessage only needs MarkdownContent stubbed out.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({ MarkdownContent: () => null }));

// Reproduces the wrapper that ChatPanel.renderPendingMessages builds around an
// optimistic bubble. renderUserMessage returns a *keyed* vnode, so Mithril
// requires every sibling in the wrapper's children array to be keyed too --
// Vnode.normalizeChildren throws a TypeError if a children array mixes keyed and
// unkeyed vnodes, or contains a null hole alongside keyed nodes. This mirrors the
// production construction so that mix is caught as a regression.
function renderPendingWrapper(id: string, isSending: boolean): m.Vnode {
  const bubble = renderUserMessage({
    type: "user_message",
    event_id: id,
    content: "hello",
    role: "user",
    source: "pending",
    timestamp: "",
  });
  if (bubble === null) throw new Error("expected a bubble for a plain pending message");
  const children: m.Vnode[] = [bubble];
  if (isSending) {
    children.push(m("div", { key: `pending-status-${id}`, class: "pending-message-status" }, "Sending…"));
  }
  return m(
    "div",
    {
      key: `pending-wrap-${id}`,
      class: isSending ? "pending-message pending-message--sending" : "pending-message",
    },
    children,
  );
}

describe("pending message wrapper keying", () => {
  it("does not throw when constructing a sending bubble (keyed bubble + status sibling)", () => {
    expect(() => renderPendingWrapper("pending-1", true)).not.toThrow();
  });

  it("does not throw when constructing a delivered bubble (keyed bubble, no status sibling)", () => {
    expect(() => renderPendingWrapper("pending-1", false)).not.toThrow();
  });

  it("keeps every wrapper child keyed in the sending state", () => {
    const wrapper = renderPendingWrapper("pending-1", true);
    const children = wrapper.children as m.Vnode[];
    expect(children).toHaveLength(2);
    for (const child of children) {
      expect(child).not.toBeNull();
      expect(child.key).toBeDefined();
    }
  });
});
