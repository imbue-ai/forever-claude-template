/**
 * Reloading the whole system interface into a freshly-built bundle.
 *
 * The frontend-reveal step of the `update-system-interface` flow rebuilds the
 * (gitignored) static bundle and then broadcasts a `reload_system_interface`
 * layout op. The dockview shell handles that op by calling `reloadInterface()`,
 * which reloads the top-level page so the browser picks up the new hashed
 * assets (and any change to the shell chrome itself), transitively reloading
 * every child chat iframe.
 */

/** Reload the top-level page that hosts the system interface.
 *
 * In the real deployment the shell IS the top-level page, so `window.top` and
 * `window` are the same frame. We still target `window.top` so the reload
 * reaches the outermost frame if the shell is ever embedded -- but a cross-origin
 * embedding makes `window.top.location` throw a `SecurityError`, so we wrap it
 * and fall back to reloading our own frame. */
export function reloadInterface(): void {
  try {
    const top = window.top;
    if (top !== null) {
      top.location.reload();
      return;
    }
  } catch {
    // Cross-origin top frame: fall through to reloading our own window.
  }
  window.location.reload();
}
