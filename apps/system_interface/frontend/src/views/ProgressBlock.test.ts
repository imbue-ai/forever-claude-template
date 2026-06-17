import { describe, expect, it, vi } from "vitest";
import type m from "mithril";
import type { AssistantMessageEvent, ToolResultEvent } from "../models/Response";
import type { StepNode, TimelineItem } from "./turn-grouping";

// Mithril captures requestAnimationFrame at import time; polyfill before imports.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// ProgressBlock pulls in the message-renderer graph (markdown + dockview). Stub
// the heavy leaves so the test exercises the real timeline/expansion logic. The
// streaming preview is wrapped in a class we can assert on, so MarkdownContent
// returning null does not hide it.
vi.mock("./DockviewWorkspace", () => ({ openSubagentTab: vi.fn() }));
vi.mock("../markdown", () => ({
  MarkdownContent: () => null,
  renderMarkdown: (s: string) => s,
}));

import { ProgressBlock } from "./ProgressBlock";

function step(overrides: Partial<StepNode> = {}): StepNode {
  return {
    ticket_id: "s1",
    title: "Do it",
    status: "active",
    summary: null,
    narration: null,
    is_carryover: false,
    is_frontier: true,
    events: [],
    ...overrides,
  };
}

function workEvent(id: string): AssistantMessageEvent {
  return {
    timestamp: id,
    type: "assistant_message",
    event_id: id,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [{ tool_call_id: `tc-${id}`, tool_name: "Read", input_preview: `{"path":"x"}` }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
  };
}

function vnodeClass(node: unknown): string {
  if (node === null || typeof node !== "object") return "";
  return ((node as m.Vnode).attrs as { className?: string } | null)?.className ?? "";
}

function hasClass(node: unknown, cls: string): boolean {
  return vnodeClass(node).split(/\s+/).includes(cls);
}

/** Depth-first: does any vnode in the tree carry the class? */
function hasClassInTree(node: unknown, cls: string): boolean {
  if (node === null || typeof node !== "object") return false;
  if (hasClass(node, cls)) return true;
  const children = (node as m.Vnode).children;
  if (Array.isArray(children)) return children.some((c) => hasClassInTree(c, cls));
  return hasClassInTree(children, cls);
}

/** Depth-first: find the first vnode matching tag + class. */
function findByTagClass(node: unknown, tag: string, cls: string): m.Vnode | null {
  if (node === null || typeof node !== "object") return null;
  const vnode = node as m.Vnode;
  if (vnode.tag === tag && hasClass(vnode, cls)) return vnode;
  const children = vnode.children;
  const list = Array.isArray(children) ? children : [children];
  for (const c of list) {
    const found = findByTagClass(c, tag, cls);
    if (found !== null) return found;
  }
  return null;
}

const STREAMING_CLASS = "pv-expanded-streaming";

interface BlockAttrs {
  items: TimelineItem[];
  trailing_reply: AssistantMessageEvent[];
  toolResults: Map<string, ToolResultEvent>;
  agentId: string;
  streamingPreview: string | null;
}

/** ProgressBlock's view typed for the test: invoking a mithril component's view
 *  directly trips its deeply-generic Vnode types, so view through a structural
 *  shape that accepts just the attrs the component reads. */
type ViewableBlock = { view: (vnode: { attrs: BlockAttrs }) => m.Vnode };

/** Render a ProgressBlock holding a single step, optionally expanding that step
 *  first (the live stream only renders inside an expanded body). */
function renderWithStep(stepNode: StepNode, streamingPreview: string | null, expand: boolean): m.Vnode {
  const attrs: BlockAttrs = {
    items: [{ kind: "step", step: stepNode }],
    trailing_reply: [],
    toolResults: new Map<string, ToolResultEvent>(),
    agentId: "agent-1",
    streamingPreview,
  };
  const component = ProgressBlock() as unknown as ViewableBlock;
  const first = component.view({ attrs });
  if (!expand) return first;
  const titleButton = findByTagClass(first, "button", "pv-tl-title");
  const onclick = (titleButton?.attrs as { onclick?: () => void } | undefined)?.onclick;
  if (onclick === undefined) {
    // Surfacing this (rather than silently returning) catches a regression where
    // the frontier step is wrongly non-expandable while streaming.
    throw new Error("frontier step title is not expandable");
  }
  onclick();
  return component.view({ attrs });
}

describe("ProgressBlock streaming preview", () => {
  it("renders the live stream inside an expanded frontier step's body", () => {
    const tree = renderWithStep(step({ events: [workEvent("w1")] }), "typing a response...", /* expand */ true);
    expect(hasClassInTree(tree, STREAMING_CLASS)).toBe(true);
  });

  it("does not render the live stream while the frontier step is collapsed", () => {
    const tree = renderWithStep(step({ events: [workEvent("w1")] }), "typing a response...", /* expand */ false);
    expect(hasClassInTree(tree, STREAMING_CLASS)).toBe(false);
  });

  it("makes a frontier step expandable even with no finalized work yet", () => {
    // A step whose very first content is still streaming has no events; it must
    // still be openable so the user can watch the live output.
    const tree = renderWithStep(step({ events: [] }), "first words...", /* expand */ true);
    expect(hasClassInTree(tree, STREAMING_CLASS)).toBe(true);
  });

  it("ignores the preview for a non-frontier step", () => {
    // A settled (non-frontier) step with work is expandable, but the live stream
    // belongs only to the frontier step, so it must not appear here.
    const tree = renderWithStep(
      step({ is_frontier: false, events: [workEvent("w1")] }),
      "typing a response...",
      /* expand */ true,
    );
    expect(hasClassInTree(tree, STREAMING_CLASS)).toBe(false);
  });

  it("renders no stream when there is no preview", () => {
    const tree = renderWithStep(step({ events: [workEvent("w1")] }), null, /* expand */ true);
    expect(hasClassInTree(tree, STREAMING_CLASS)).toBe(false);
  });
});
