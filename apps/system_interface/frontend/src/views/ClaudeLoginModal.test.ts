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

import { ClaudeLoginModal, computeDesktopAppOrigin } from "./ClaudeLoginModal";

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

function findById(tree: unknown, id: string): VnodeLike | undefined {
  for (const vnode of walk(tree)) {
    if (vnode.attrs?.id === id) return vnode;
  }
  return undefined;
}

// Find a `<button>` vnode whose rendered text contains `text` and invoke its
// onclick. Matches on the vnode tag (the hyperscript selector) so it picks the
// button itself rather than an ancestor container that merely contains the text.
function clickButtonByText(tree: unknown, text: string): void {
  for (const vnode of walk(tree)) {
    const onclick = vnode.attrs?.onclick;
    const tag = (vnode as { tag?: unknown }).tag;
    if (
      typeof onclick === "function" &&
      typeof tag === "string" &&
      tag.startsWith("button") &&
      JSON.stringify(vnode.children ?? "").includes(text)
    ) {
      (onclick as () => void)();
      return;
    }
  }
  throw new Error(`No button found with text: ${text}`);
}

// Let queued microtasks + the setTimeout-based redraw polyfill drain.
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

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
    expect(tree).not.toContain("Sign in with Imbue");
    expect(tree).not.toContain("Use an API key");
  });

  it("reveals the Imbue and API-key options when the disclosure is expanded", () => {
    const modal = makeModal();
    const toggle = findByClass(modal.render(), "claude-login-alts-toggle");
    expect(toggle).toBeDefined();

    const onclick = toggle?.attrs?.onclick;
    expect(typeof onclick).toBe("function");
    (onclick as () => void)();

    const expanded = JSON.stringify(modal.render());
    // Imbue is listed before the raw API-key option (the house offering).
    expect(expanded).toContain("Sign in with Imbue");
    expect(expanded).toContain("Use an API key");
    expect(expanded.indexOf("Sign in with Imbue")).toBeLessThan(expanded.indexOf("Use an API key"));
    // Console OAuth is gone entirely.
    expect(expanded).not.toContain("Anthropic Console");
  });

  it("shows the Imbue paste form with the mint-page link and textarea", () => {
    const modal = makeModal();
    const toggle = findByClass(modal.render(), "claude-login-alts-toggle");
    (toggle?.attrs?.onclick as () => void)();
    clickButtonByText(modal.render(), "Sign in with Imbue");
    const tree = JSON.stringify(modal.render());
    expect(tree).toContain("Open the Imbue key page");
    expect(findById(modal.render(), "claude-login-imbue-blob-input")).toBeDefined();
  });

  it("offers only a Start-over action on a sign-in failure, and it restarts the flow", async () => {
    // The node test env has no XHR, so the OAuth-start request fails -- landing
    // on the same `error` view that a failed OAuth code submission now routes to
    // (a submitted code consumes the single-use session, so it cannot be retried
    // in place). The failure screen must expose only a working "Start over" (the
    // old dead "Try again"/code-form path is gone), and "Start over" must return
    // to the beginning of sign-in.
    const modal = makeModal();
    clickButtonByText(modal.render(), "Continue with Claude subscription");
    await flush();

    const failed = modal.render();
    const serialized = JSON.stringify(failed);
    expect(serialized).toContain("Start over");
    // The pre-fix retry affordances are gone from the failure screen.
    expect(serialized).not.toContain("Try again");
    expect(findById(failed, "claude-login-code-input")).toBeUndefined();

    // "Start over" returns to the beginning of sign-in (provider selection).
    clickButtonByText(failed, "Start over");
    await flush();
    expect(JSON.stringify(modal.render())).toContain("Continue with Claude subscription");
  });
});

describe("computeDesktopAppOrigin", () => {
  it("drops the agent label from a workspace .localhost hostname", () => {
    expect(computeDesktopAppOrigin("agent-abc123.localhost", "8420", "http:")).toBe("http://localhost:8420");
  });

  it("keeps a bare localhost hostname", () => {
    expect(computeDesktopAppOrigin("localhost", "8420", "http:")).toBe("http://localhost:8420");
  });

  it("returns null for remote (tunneled) hostnames", () => {
    expect(computeDesktopAppOrigin("web--agent--user.example.com", "", "https:")).toBeNull();
  });
});
