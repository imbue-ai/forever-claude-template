import { describe, expect, it, vi } from "vitest";
import type m from "mithril";
import type { UserMessageEvent } from "../models/Response";

// apiUrl reads a <meta> tag via document, absent in the node test environment.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));
// message-renderers pulls in the dockview/markdown module graph; stub them so
// this test exercises only the bubble renderer.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({ MarkdownContent: () => null }));

import { StableUserMessage } from "./message-renderers";

const IMAGE_PATH = "/code/uploads/aaa/diagram.png";

interface CollectedTag {
  tag: string;
  className: string;
  src: unknown;
}

function collect(node: unknown, texts: string[], tags: CollectedTag[]): void {
  if (node === null || node === undefined || node === false || node === true) {
    return;
  }
  if (typeof node === "string" || typeof node === "number") {
    texts.push(String(node));
    return;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      collect(child, texts, tags);
    }
    return;
  }
  const vnode = node as { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown };
  if (vnode.tag === "#") {
    texts.push(String(vnode.children));
    return;
  }
  if (typeof vnode.tag === "string") {
    tags.push({
      tag: vnode.tag,
      className: String(vnode.attrs?.className ?? ""),
      src: vnode.attrs?.src,
    });
  }
  if (vnode.text !== undefined && vnode.text !== null) {
    texts.push(String(vnode.text));
  }
  collect(vnode.children, texts, tags);
}

function renderBubble(content: string): { texts: string[]; tags: CollectedTag[] } {
  const event: UserMessageEvent = {
    type: "user_message",
    event_id: "e1",
    content,
    role: "user",
    source: "test",
    timestamp: "",
  };
  const component = StableUserMessage();
  const vnode = component.view({ attrs: { event } } as unknown as m.Vnode<{ event: UserMessageEvent }>);
  const texts: string[] = [];
  const tags: CollectedTag[] = [];
  collect(vnode, texts, tags);
  return { texts, tags };
}

describe("user message bubble with attachments", () => {
  it("shows the typed text but hides the appended path line", () => {
    const { texts } = renderBubble(`here is the file\n\nSee attachment here: ${IMAGE_PATH}`);
    const allText = texts.join(" ");

    expect(allText).toContain("here is the file");
    expect(allText).not.toContain("See attachment here");
    expect(allText).not.toContain(IMAGE_PATH);
  });

  it("renders an image thumbnail for an image attachment", () => {
    const { tags } = renderBubble(`look\n\nSee attachment here: ${IMAGE_PATH}`);

    const image = tags.find((t) => t.tag === "img" && t.className.includes("message-attachment-image"));
    expect(image).toBeDefined();
    expect(image?.src).toBe("/api/uploads/aaa/diagram.png");
  });

  it("leaves an ordinary message free of attachment markup", () => {
    const { tags } = renderBubble("just talking");
    expect(tags.some((t) => t.className.includes("message-attachment"))).toBe(false);
  });
});
