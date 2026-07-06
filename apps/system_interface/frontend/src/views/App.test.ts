import { describe, expect, it } from "vitest";

import { App } from "./App";

// App's view() ignores its vnode argument; mithril's Component type still
// requires one, so pass a minimal stand-in.
function renderApp(): unknown {
  const component = App();
  return component.view({} as Parameters<typeof component.view>[0]);
}

describe("App", () => {
  it("renders its view", () => {
    expect(() => renderApp()).not.toThrow();
  });
});
