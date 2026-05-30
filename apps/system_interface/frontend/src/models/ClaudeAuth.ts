/**
 * Global Claude auth-state for the in-UI login modal.
 *
 * Claude auth is mind-global: every agent reads the same host env file and
 * `CLAUDE_CONFIG_DIR`, so a broken auth state is never per-agent -- if one
 * agent is logged out, they all are. A single module-level `loginModalOpen`
 * flag therefore drives one shared `ClaudeLoginModal` (rendered once in
 * `App.ts`), rather than every `ChatPanel` subscribing and tracking its own
 * modal state.
 *
 * `openLoginModal` is called whenever any agent's transcript surfaces an
 * auth-error -- live over the SSE stream, or detected when a panel loads a
 * snapshot. `closeLoginModal` is the modal's dismiss handler. A fresh
 * auth-error after a dismiss reopens the modal.
 */

import m from "mithril";

let loginModalOpen = false;

export function isLoginModalOpen(): boolean {
  return loginModalOpen;
}

export function openLoginModal(): void {
  if (loginModalOpen) return;
  loginModalOpen = true;
  m.redraw();
}

export function closeLoginModal(): void {
  if (!loginModalOpen) return;
  loginModalOpen = false;
  m.redraw();
}
