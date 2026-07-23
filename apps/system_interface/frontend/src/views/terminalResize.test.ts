import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { wireTerminalIframeRefit, type TerminalPanelResizeApi } from "./terminalResize";

// The wiring only touches querySelectorAll / getBoundingClientRect on the
// container and contentWindow.term.fit on each iframe, so structural fakes
// suffice in the node test environment. The actual browser behavior (whether
// the refit reaches the PTY across ttyd reconnects) is verified against the
// real desktop client, not modeled here.

interface FakeIframe {
  contentWindow: { term?: { fit?: () => void } } | null;
}

function makeContainer(iframes: FakeIframe[], size: { width: number; height: number }) {
  return {
    querySelectorAll: () => iframes,
    getBoundingClientRect: () => ({ width: size.width, height: size.height }),
    size,
  } as unknown as HTMLElement & { size: { width: number; height: number } };
}

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  observed: unknown[] = [];
  disconnected = false;
  constructor(private callback: () => void) {
    FakeResizeObserver.instances.push(this);
  }
  observe(target: unknown) {
    this.observed.push(target);
  }
  disconnect() {
    this.disconnected = true;
  }
  fire() {
    this.callback();
  }
}

function makeApi(isVisible: boolean) {
  let listener: ((event: { isVisible: boolean }) => void) | null = null;
  const api: TerminalPanelResizeApi & { setVisible(v: boolean): void; unhooked: boolean } = {
    isVisible,
    unhooked: false,
    onDidVisibilityChange(fn) {
      listener = fn;
      return {
        dispose: () => {
          api.unhooked = true;
        },
      };
    },
    setVisible(v: boolean) {
      (api as { isVisible: boolean }).isVisible = v;
      listener?.({ isVisible: v });
    },
  };
  return api;
}

describe("wireTerminalIframeRefit", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeResizeObserver.instances = [];
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("refits on resize signals and again on the trailing re-checks", () => {
    const fit = vi.fn();
    const container = makeContainer([{ contentWindow: { term: { fit } } }], { width: 800, height: 600 });
    wireTerminalIframeRefit(container, makeApi(true));

    FakeResizeObserver.instances[0].fire();
    expect(fit).toHaveBeenCalledTimes(1);

    vi.runAllTimers();
    // one immediate call plus one per trailing delay
    expect(fit.mock.calls.length).toBeGreaterThan(1);
  });

  it("does not refit while the panel is hidden or collapsed to zero", () => {
    const fit = vi.fn();
    const hiddenApi = makeApi(false);
    const container = makeContainer([{ contentWindow: { term: { fit } } }], { width: 800, height: 600 });
    wireTerminalIframeRefit(container, hiddenApi);
    FakeResizeObserver.instances[0].fire();
    vi.runAllTimers();
    expect(fit).not.toHaveBeenCalled();

    const zeroContainer = makeContainer([{ contentWindow: { term: { fit } } }], { width: 0, height: 0 });
    wireTerminalIframeRefit(zeroContainer, makeApi(true));
    FakeResizeObserver.instances[1].fire();
    vi.runAllTimers();
    expect(fit).not.toHaveBeenCalled();
  });

  it("refits when the panel becomes visible", () => {
    const fit = vi.fn();
    const api = makeApi(false);
    const container = makeContainer([{ contentWindow: { term: { fit } } }], { width: 800, height: 600 });
    wireTerminalIframeRefit(container, api);
    api.setVisible(true);
    expect(fit).toHaveBeenCalledTimes(1);
  });

  it("tolerates iframes whose client has not exposed term yet", () => {
    const container = makeContainer([{ contentWindow: {} }, { contentWindow: null }], { width: 800, height: 600 });
    wireTerminalIframeRefit(container, makeApi(true));
    expect(() => FakeResizeObserver.instances[0].fire()).not.toThrow();
  });

  it("skips cross-origin iframes (contentWindow access throws) and still refits the rest", () => {
    const fit = vi.fn();
    const crossOrigin = {
      get contentWindow(): never {
        throw new DOMException("Blocked a frame from accessing a cross-origin frame.", "SecurityError");
      },
    } as unknown as FakeIframe;
    const container = makeContainer([crossOrigin, { contentWindow: { term: { fit } } }], {
      width: 800,
      height: 600,
    });
    wireTerminalIframeRefit(container, makeApi(true));
    expect(() => FakeResizeObserver.instances[0].fire()).not.toThrow();
    expect(fit).toHaveBeenCalledTimes(1);
  });

  it("dispose cancels trailing re-checks and unhooks everything", () => {
    const fit = vi.fn();
    const api = makeApi(true);
    const container = makeContainer([{ contentWindow: { term: { fit } } }], { width: 800, height: 600 });
    const wired = wireTerminalIframeRefit(container, api);

    FakeResizeObserver.instances[0].fire();
    expect(fit).toHaveBeenCalledTimes(1);

    wired.dispose();
    vi.runAllTimers();
    expect(fit).toHaveBeenCalledTimes(1);
    expect(FakeResizeObserver.instances[0].disconnected).toBe(true);
    expect(api.unhooked).toBe(true);
  });
});
