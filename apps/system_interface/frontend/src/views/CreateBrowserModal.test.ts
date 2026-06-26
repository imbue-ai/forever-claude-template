import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws; the modal calls `m.redraw()` from its submit handler. Vitest's
// default (node) environment has no such global, so polyfill it before any
// import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { CreateBrowserModal } from "./CreateBrowserModal";

type VnodeLike = {
  tag?: unknown;
  attrs?: Record<string, unknown>;
  children?: unknown;
  text?: unknown;
};

// Depth-first walk over a rendered Mithril vnode tree.
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

// Find the first vnode of the given tag whose normalized className includes the
// class fragment. Mithril splits `m("button.foo")` into `tag: "button"` and
// `attrs.className: "foo"`, so matching must consult both.
function findByTagAndClass(tree: unknown, tag: string, classFragment: string): VnodeLike | undefined {
  for (const vnode of walk(tree)) {
    if (vnode.tag !== tag) continue;
    const className = vnode.attrs?.className;
    if (typeof className === "string" && className.split(/\s+/).includes(classFragment)) return vnode;
  }
  return undefined;
}

interface Calls {
  accepted: string[];
  created: string[];
  // Each failure records the name and the ``createdPane`` flag the modal
  // forwarded, so a test can assert the parent only tears down panes it made.
  failed: Array<{ name: string; createdPane: boolean }>;
  cancelled: number;
}

function makeModal(opts?: { existingBrowserNames?: string[]; acceptCreatesPane?: boolean }): {
  render: () => unknown;
  calls: Calls;
  attrs: Parameters<ReturnType<typeof CreateBrowserModal>["view"]>[0]["attrs"];
} {
  const component = CreateBrowserModal();
  const calls: Calls = { accepted: [], created: [], failed: [], cancelled: 0 };
  // Default to "accept created a new pane" so existing assertions are unchanged;
  // a test can flip this to model an open that deduped onto an existing pane.
  const acceptCreatesPane = opts?.acceptCreatesPane ?? true;
  const attrs = {
    browserServiceUrl: "/service/browser/",
    existingBrowserNames: opts?.existingBrowserNames ?? [],
    onAccept: (name: string): boolean => {
      calls.accepted.push(name);
      return acceptCreatesPane;
    },
    onCreated: (name: string): void => {
      calls.created.push(name);
    },
    onFailed: (name: string, createdPane: boolean): void => {
      calls.failed.push({ name, createdPane });
    },
    onCancel: (): void => {
      calls.cancelled += 1;
    },
  };
  // The view reads closure state; a minimal vnode stand-in is sufficient.
  const vnode = { attrs };
  return {
    render: () => component.view(vnode as unknown as Parameters<typeof component.view>[0]),
    calls,
    attrs,
  };
}

// Type the input's value into the modal by invoking its `oninput` handler.
function typeName(modal: ReturnType<typeof makeModal>, value: string): void {
  const input = findByTagAndClass(modal.render(), "input", "custom-url-dialog-input");
  const oninput = input?.attrs?.oninput as ((e: { target: { value: string } }) => void) | undefined;
  expect(typeof oninput).toBe("function");
  oninput!({ target: { value } });
}

function clickCreate(modal: ReturnType<typeof makeModal>): void {
  const button = findByTagAndClass(modal.render(), "button", "custom-url-dialog-open");
  const onclick = button?.attrs?.onclick as (() => void) | undefined;
  expect(typeof onclick).toBe("function");
  onclick!();
}

describe("CreateBrowserModal", () => {
  beforeEach(() => {
    // Default: random-name prefill fetch and create POST both stubbed per-test.
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders a 'New browser' title and a name input", () => {
    const tree = JSON.stringify(makeModal().render());
    expect(tree).toContain("New browser");
    expect(tree).toContain("Browser Name");
  });

  it("opens the optimistic pane on accept and posts {name} to the daemon", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ name: "alex-smith", key_available: true }) });
    vi.stubGlobal("fetch", fetchMock);

    const modal = makeModal();
    typeName(modal, "alex-smith");
    clickCreate(modal);

    // onAccept fires synchronously, before the POST resolves: the optimistic
    // 'starting' pane opens immediately.
    expect(modal.calls.accepted).toEqual(["alex-smith"]);

    // Let the awaited POST settle.
    await vi.waitFor(() => expect(modal.calls.created).toEqual(["alex-smith"]));

    expect(fetchMock).toHaveBeenCalledWith(
      "/service/browser/browsers",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "alex-smith" }) }),
    );
    expect(modal.calls.failed).toEqual([]);
  });

  it("surfaces a 409 error inline and tears down the optimistic pane", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ error: "3/3 browsers open -- close one first." }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const modal = makeModal();
    typeName(modal, "my-browser");
    clickCreate(modal);

    expect(modal.calls.accepted).toEqual(["my-browser"]);
    await vi.waitFor(() => expect(modal.calls.failed).toEqual([{ name: "my-browser", createdPane: true }]));

    // The daemon's message is shown verbatim and the modal stays open (no
    // onCreated, no auto-close).
    expect(JSON.stringify(modal.render())).toContain("3/3 browsers open -- close one first.");
    expect(modal.calls.created).toEqual([]);
  });

  it("does nothing when the name is blank", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const modal = makeModal();
    typeName(modal, "   ");
    clickCreate(modal);

    expect(modal.calls.accepted).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("pre-validates a duplicate name inline without opening a pane or posting", () => {
    // Layer one: a name that already exists must be rejected BEFORE onAccept, so
    // the optimistic open never runs (and so never dedups onto -- then closes --
    // the existing browser's healthy pane).
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const modal = makeModal({ existingBrowserNames: ["alex-smith"] });
    typeName(modal, "alex-smith");
    clickCreate(modal);

    expect(modal.calls.accepted).toEqual([]);
    expect(modal.calls.failed).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(JSON.stringify(modal.render())).toContain("A browser named alex-smith already exists");
  });

  it("forwards createdPane=false to onFailed when the open deduped onto an existing pane", async () => {
    // Layer two (defense in depth): even if a create somehow fails after the
    // open deduped onto a pre-existing pane (acceptCreatesPane=false), the modal
    // reports createdPane=false so the parent leaves that healthy pane alone.
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ error: "a browser named my-browser already exists" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const modal = makeModal({ acceptCreatesPane: false });
    typeName(modal, "my-browser");
    clickCreate(modal);

    expect(modal.calls.accepted).toEqual(["my-browser"]);
    await vi.waitFor(() => expect(modal.calls.failed).toEqual([{ name: "my-browser", createdPane: false }]));
  });
});
