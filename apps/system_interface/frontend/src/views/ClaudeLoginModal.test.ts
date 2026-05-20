import { describe, expect, it } from "vitest";
import { ClaudeLoginModal } from "./ClaudeLoginModal";

/**
 * The modal opens in `select_provider` mode by default. Its view builds a
 * fragment whose children mixed an unkeyed intro `<p>` with keyed provider
 * `<button>`s -- Mithril's hyperscript rejects mixed keyed/unkeyed fragments
 * and throws synchronously from `m()`, which aborted the whole render so the
 * modal never appeared on screen. These tests render the view directly (no
 * DOM required, since the throw happens during vnode construction) to lock
 * the invariant down.
 */
describe("ClaudeLoginModal", () => {
  function renderView(): unknown {
    const component = ClaudeLoginModal();
    // The view ignores its vnode argument (it reads closure state), so a
    // minimal stand-in cast to the expected parameter type is sufficient.
    const vnode = { attrs: { chatAgentName: null, onDismiss: () => {} } };
    return component.view(vnode as Parameters<typeof component.view>[0]);
  }

  it("renders its default provider-selection view without a mixed-key fragment error", () => {
    expect(() => renderView()).not.toThrow();
  });

  it("produces all three provider options", () => {
    const tree = JSON.stringify(renderView());
    expect(tree).toContain("claude-login-provider");
    expect(tree).toContain("Claude subscription");
    expect(tree).toContain("Anthropic Console");
    expect(tree).toContain("Use an API key");
  });
});
