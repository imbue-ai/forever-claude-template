import { describe, expect, it, vi, beforeEach } from "vitest";

// The card click delegates to the InputDraft store; mock it so a test can assert
// the prefill is handed over without pulling in mithril's redraw machinery.
const setInputDraft = vi.fn();
vi.mock("../models/InputDraft", () => ({
  setInputDraft: (agentId: string, text: string) => setInputDraft(agentId, text),
}));

import { parseChoicesJson, parseChoiceSegments, renderChoiceCards, type Choice } from "./choice-cards";

beforeEach(() => {
  setInputDraft.mockClear();
});

// Recursively gather every string in a mithril vnode tree (text + children).
function allText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(allText).join(" ");
  if (typeof node === "object") {
    const v = node as { text?: unknown; children?: unknown };
    return `${allText(v.text)} ${allText(v.children)}`;
  }
  return "";
}

// Collect every vnode in the tree whose tag matches.
function collectByTag(node: unknown, tag: string): { attrs?: Record<string, unknown> }[] {
  if (node == null) return [];
  if (Array.isArray(node)) return node.flatMap((n) => collectByTag(n, tag));
  if (typeof node === "object") {
    const v = node as { tag?: unknown; children?: unknown; attrs?: Record<string, unknown> };
    const self = v.tag === tag ? [v as { attrs?: Record<string, unknown> }] : [];
    return [...self, ...collectByTag(v.children, tag)];
  }
  return [];
}

const CARDS_BLOCK = [
  "```minds-choices",
  '[{"title": "Consolidate your messages", "subtitle": "All in one place.", "prefill": "Help me consolidate."},',
  ' {"title": "Suggest a few things", "prefill": "Suggest a few things I could work on."}]',
  "```",
].join("\n");

describe("parseChoicesJson", () => {
  it("parses a well-formed array with optional subtitles", () => {
    const choices = parseChoicesJson(
      '[{"title": "A", "prefill": "do a", "subtitle": "sub a"}, {"title": "B", "prefill": "do b"}]',
    );
    expect(choices).toEqual<Choice[]>([
      { title: "A", prefill: "do a", subtitle: "sub a" },
      { title: "B", prefill: "do b" },
    ]);
  });

  it("rejects invalid JSON", () => {
    expect(parseChoicesJson("not json")).toBeNull();
  });

  it("rejects a non-array, an empty array, and items missing required fields", () => {
    expect(parseChoicesJson('{"title": "A", "prefill": "x"}')).toBeNull();
    expect(parseChoicesJson("[]")).toBeNull();
    expect(parseChoicesJson('[{"title": "A"}]')).toBeNull();
    expect(parseChoicesJson('[{"prefill": "x"}]')).toBeNull();
    expect(parseChoicesJson('[{"title": 1, "prefill": "x"}]')).toBeNull();
  });
});

describe("parseChoiceSegments", () => {
  it("returns a single markdown segment when there is no marker", () => {
    const segments = parseChoiceSegments("Hello **world**");
    expect(segments).toEqual([{ kind: "markdown", text: "Hello **world**" }]);
  });

  it("splits prose around a choices block in order", () => {
    const text = `Some intro prose.\n\n${CARDS_BLOCK}\n\nTrailing prose.`;
    const segments = parseChoiceSegments(text);
    expect(segments.map((s) => s.kind)).toEqual(["markdown", "choices", "markdown"]);
    expect(segments[0]).toMatchObject({ kind: "markdown", text: "Some intro prose." });
    expect(segments[2]).toMatchObject({ kind: "markdown", text: "Trailing prose." });
    if (segments[1].kind !== "choices") throw new Error("expected choices segment");
    expect(segments[1].choices).toHaveLength(2);
    expect(segments[1].choices[1].prefill).toBe("Suggest a few things I could work on.");
  });

  it("leaves a malformed choices block as visible markdown rather than dropping it", () => {
    const text = "```minds-choices\nnot valid json\n```";
    const segments = parseChoiceSegments(text);
    expect(segments).toEqual([{ kind: "markdown", text }]);
  });

  it("leaves an unterminated fence as markdown", () => {
    const text = '```minds-choices\n[{"title": "A", "prefill": "x"}]';
    const segments = parseChoiceSegments(text);
    expect(segments.map((s) => s.kind)).toEqual(["markdown"]);
  });
});

describe("renderChoiceCards", () => {
  const choices: Choice[] = [
    { title: "Consolidate your messages", subtitle: "All in one place.", prefill: "Help me consolidate." },
    { title: "Suggest a few things", prefill: "Suggest a few things I could work on." },
  ];

  it("renders one card per choice with title and subtitle text", () => {
    const vnode = renderChoiceCards(choices, "agent-1");
    const text = allText(vnode);
    expect(text).toContain("Consolidate your messages");
    expect(text).toContain("All in one place.");
    expect(text).toContain("Suggest a few things");
    expect(collectByTag(vnode, "button")).toHaveLength(2);
  });

  it("prefills the composer with the clicked card's phrase (and never auto-sends)", () => {
    const vnode = renderChoiceCards(choices, "agent-1");
    const buttons = collectByTag(vnode, "button");
    const onclick = buttons[1].attrs?.onclick as (e: Event) => void;
    onclick({ preventDefault() {} } as unknown as Event);
    expect(setInputDraft).toHaveBeenCalledTimes(1);
    expect(setInputDraft).toHaveBeenCalledWith("agent-1", "Suggest a few things I could work on.");
  });

  it("passes an empty prefill through unchanged (focus-only card)", () => {
    const vnode = renderChoiceCards([{ title: "I have something in mind", prefill: "" }], "agent-9");
    const onclick = collectByTag(vnode, "button")[0].attrs?.onclick as (e: Event) => void;
    onclick({ preventDefault() {} } as unknown as Event);
    expect(setInputDraft).toHaveBeenCalledWith("agent-9", "");
  });
});
