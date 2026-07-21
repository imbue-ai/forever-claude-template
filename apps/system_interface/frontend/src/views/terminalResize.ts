/**
 * Keep ttyd terminal tabs reflowing to their container even when the ttyd
 * client's own refit trigger is momentarily dead.
 *
 * The ttyd web client refits its xterm grid -- and pushes the new cols/rows
 * over its websocket, which is what makes the PTY (and e.g. `claude`) reflow --
 * from exactly one trigger: a `resize` event on the iframe's window, handled by
 * `fitAddon.fit()`. That listener is NOT always alive: on any websocket close
 * the client runs `dispose()` (dropping the resize listener and the
 * onResize->PTY push) and only re-registers listeners once the reconnect
 * completes. Terminal websockets in the desktop client traverse the latchkey
 * forward and drop intermittently (observed close code 1006), so a sash drag
 * or tab activation that lands inside a reconnect window is silently lost: the
 * terminal keeps its stale cols, the reconnect handshake then tells the new PTY
 * that stale size, and nothing re-measures until the user resizes again. That
 * is the intermittent "terminal does not reflow until I click into it and drag
 * again" bug.
 *
 * The host page can heal all of this because `fit()` is pure client-side
 * geometry: it works while the socket is down, and the ttyd client sends its
 * *current* cols/rows in the reconnect handshake. So we refit from out here,
 * driven by a ResizeObserver on the panel's container (fires after layout with
 * the real box, for both sash drags and the collapsed->full growth when a tab
 * is activated), with a visibility-change safety net and short trailing
 * re-checks to self-correct any transiently-wrong read (e.g. the iframe's
 * inner layout settling a frame after the host's). Calling `fit()` when
 * nothing changed is a no-op, so the extra invocations are free.
 *
 * Wired for terminal panels only (both agent terminals and persistent session
 * terminals); non-terminal iframes get no resize churn.
 */

/** Structural view of the dockview panel-api signals this wiring consumes.
 *  Kept minimal (and dockview-free) so the wiring is unit-testable with a fake
 *  api; the real `DockviewPanelApi` satisfies it structurally. */
export interface TerminalPanelResizeApi {
  readonly isVisible: boolean;
  onDidVisibilityChange(listener: (event: { isVisible: boolean }) => void): { dispose: () => void };
}

/** The subset of the ttyd client's exposed terminal object we call. */
interface TtydTerminalWindow extends Window {
  term?: { fit?: () => void };
}

/** Trailing re-check delays after the last resize/visibility signal. The first
 *  covers the iframe's inner layout settling just after the host's; the later
 *  ones re-assert the size across a ttyd reconnect completing shortly after a
 *  drag (reconnects observed while dragging settle well within this span). */
const TRAILING_REFIT_DELAYS_MS = [50, 250, 1000];

/** Refit every same-origin ttyd iframe under `container` via the client's own
 *  `term.fit()` (recomputes cols/rows from the live DOM and, when the socket is
 *  open, pushes the new size to the PTY; when it is closed, the corrected
 *  cols/rows still reach the PTY via the reconnect handshake). Iframes whose
 *  client has not booted far enough to expose `term` are skipped -- the boot
 *  path ends in its own `fit()`. */
export function refitTerminalIframes(container: HTMLElement): void {
  container.querySelectorAll<HTMLIFrameElement>("iframe").forEach((iframe) => {
    try {
      const win = iframe.contentWindow as TtydTerminalWindow | null;
      const fit = win?.term?.fit;
      if (typeof fit === "function") fit();
    } catch {
      // Cross-origin iframe: contentWindow is not scriptable. Terminal iframes
      // are same-origin (proxied under /service/), so nothing to do here.
    }
  });
}

/** Observe a terminal panel's container and refit its iframe(s) whenever the
 *  container's box changes or the panel becomes visible, plus trailing
 *  re-checks after each signal burst. Returns a disposable that disconnects
 *  the observer, cancels pending re-checks, and unhooks the visibility
 *  subscription. */
export function wireTerminalIframeRefit(container: HTMLElement, api: TerminalPanelResizeApi): { dispose: () => void } {
  let disposed = false;
  let trailingTimers: ReturnType<typeof setTimeout>[] = [];

  function refitIfShown(): void {
    if (disposed || !api.isVisible) return;
    // A hidden/inactive panel is collapsed to ~zero; fitting against that box
    // would squash the grid to the 2-column minimum. Only refit a real box.
    const rect = container.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    refitTerminalIframes(container);
  }

  function refitNowAndTrailing(): void {
    refitIfShown();
    for (const timer of trailingTimers) clearTimeout(timer);
    trailingTimers = TRAILING_REFIT_DELAYS_MS.map((delay) => setTimeout(refitIfShown, delay));
  }

  // Primary trigger: fires after layout with the container's true current size
  // (coalesced per frame), for both sash drags and the collapsed->full growth
  // when the tab is activated (including the initial observe() callback).
  const observer = new ResizeObserver(() => {
    refitNowAndTrailing();
  });
  observer.observe(container);

  // Safety net for environments where activation does not change the measured
  // box (the observer then never fires despite the panel being revealed).
  const visibilityDisposable = api.onDidVisibilityChange((event) => {
    if (event.isVisible) refitNowAndTrailing();
  });

  return {
    dispose(): void {
      disposed = true;
      for (const timer of trailingTimers) clearTimeout(timer);
      trailingTimers = [];
      observer.disconnect();
      visibilityDisposable.dispose();
    },
  };
}
