import { describe, expect, it, vi } from "vitest";
import type m from "mithril";
import type { UserMessageEvent } from "../models/Response";
import { buildMessageWithAttachments } from "../models/attachments";

// apiUrl reads a <meta> tag via document, absent in the node test environment.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));
// message-renderers pulls in the dockview module graph; stub it out.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
// The bubble renders the attachment block through MarkdownContent, which sets
// innerHTML imperatively in oncreate. Stub it with a marker component so the
// vnode walk can read the content it was handed without needing a DOM.
vi.mock("../markdown", () => ({ MarkdownContent: { view: () => null } }));

import { MarkdownContent } from "../markdown";
import { StableUserMessage } from "./message-renderers";

const IMAGE_PATH = "/code/uploads/aaa/diagram.png";
const FILE_PATH = "/code/uploads/bbb/notes.txt";

interface Collected {
  texts: string[];
  markdownContents: string[];
}

function collect(node: unknown, out: Collected): void {
  if (node === null || node === undefined || typeof node === "boolean") {
    return;
  }
  if (typeof node === "string" || typeof node === "number") {
    out.texts.push(String(node));
    return;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      collect(child, out);
    }
    return;
  }
  const vnode = node as { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown };
  // The attachment block is delegated to MarkdownContent; capture what it was
  // handed rather than descending (its content is rendered as innerHTML).
  if (vnode.tag === MarkdownContent) {
    out.markdownContents.push(String(vnode.attrs?.content ?? ""));
    return;
  }
  if (vnode.tag === "#") {
    out.texts.push(String(vnode.children));
    return;
  }
  if (vnode.text !== undefined && vnode.text !== null) {
    out.texts.push(String(vnode.text));
  }
  collect(vnode.children, out);
}

function renderBubble(content: string): Collected {
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
  const out: Collected = { texts: [], markdownContents: [] };
  collect(vnode, out);
  return out;
}

describe("user message bubble with attachments", () => {
  it("keeps the typed text and hands the attachment line to markdown (not hidden)", () => {
    const { texts, markdownContents } = renderBubble(buildMessageWithAttachments("here is the file", [IMAGE_PATH]));

    expect(texts.join(" ")).toContain("here is the file");
    // The path is not in the plain-text nodes: it lives inside the markdown block.
    expect(texts.join(" ")).not.toContain(IMAGE_PATH);
    expect(markdownContents).toHaveLength(1);
    expect(markdownContents[0]).toContain("See attachment here:");
    // Image: inline image markdown with the absolute path as both alt and URL.
    expect(markdownContents[0]).toContain(`![${IMAGE_PATH}](${IMAGE_PATH})`);
  });

  it("renders a non-image attachment as a download link, not an inline image", () => {
    const { markdownContents } = renderBubble(buildMessageWithAttachments("", [FILE_PATH]));

    expect(markdownContents).toHaveLength(1);
    expect(markdownContents[0]).toContain(`[${FILE_PATH}](${FILE_PATH})`);
    expect(markdownContents[0]).not.toContain(`![${FILE_PATH}]`);
  });

  it("leaves an ordinary message free of an attachment block", () => {
    const { texts, markdownContents } = renderBubble("just talking");

    expect(texts.join(" ")).toContain("just talking");
    expect(markdownContents).toHaveLength(0);
  });
});
