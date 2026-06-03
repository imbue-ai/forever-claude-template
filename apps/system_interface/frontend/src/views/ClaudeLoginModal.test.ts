import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, which
// makes the `m.redraw()` calls inside the modal's event handlers throw.
// Provide a polyfill before any import is evaluated so the handlers (e.g.
// the "Other ways to sign in" toggle) can be exercised in tests.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { ClaudeLoginModal } from "./ClaudeLoginModal";

type VnodeLike = {
  attrs?: Record<string, unknown>;
  children?: unknown;
};

function makeModal(): { render: () => unknown } {
  const component = ClaudeLoginModal();
  // The view ignores its vnode argument (it reads closure state), so a
  // minimal stand-in cast to the expected parameter type is sufficient.
  const vnode = { attrs: { onDismiss: () => {} } };
  return {
    render: () => component.view(vnode as Parameters<typeof component.view>[0]),
  };
}

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

function findByClass(tree: unknown, className: string): VnodeLike | undefined {
  for (const vnode of walk(tree)) {
    const classes = vnode.attrs?.className;
    if (typeof classes === "string" && classes.split(/\s+/).includes(className)) {
      return vnode;
    }
  }
  return undefined;
}

describe("ClaudeLoginModal", () => {
  it("renders the default provider-selection view without a mixed-key fragment error", () => {
    // Mithril's hyperscript throws synchronously from `m()` when a fragment
    // mixes keyed and unkeyed children; this locks that invariant down.
    expect(() => makeModal().render()).not.toThrow();
  });

  it("leads with the Claude subscription as the prominent default option", () => {
    const tree = JSON.stringify(makeModal().render());
    expect(tree).toContain("claude-login-primary");
    expect(tree).toContain("Sign in with your Claude subscription");
    expect(tree).toContain("Continue with Claude subscription");
  });

  it("keeps the other sign-in methods collapsed behind a disclosure", () => {
    const tree = JSON.stringify(makeModal().render());
    expect(tree).toContain("Other ways to sign in");
    // The alternatives stay hidden until the disclosure is expanded.
    expect(tree).not.toContain("Anthropic Console");
    expect(tree).not.toContain("Use an API key");
  });

  it("reveals the Console and API-key options when the disclosure is expanded", () => {
    const modal = makeModal();
    const toggle = findByClass(modal.render(), "claude-login-alts-toggle");
    expect(toggle).toBeDefined();

    const onclick = toggle?.attrs?.onclick;
    expect(typeof onclick).toBe("function");
    (onclick as () => void)();

    const expanded = JSON.stringify(modal.render());
    expect(expanded).toContain("Anthropic Console");
    expect(expanded).toContain("Use an API key");
  });
});
