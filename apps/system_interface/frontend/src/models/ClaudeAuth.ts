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
import { apiUrl } from "../base-path";

let loginModalOpen = false;

// One status probe per page load: the empty-transcript backstop below only
// needs to fire once, and a user who dismissed the modal shouldn't have every
// additional empty panel reopen it. The reactive is_auth_error path is
// unaffected by this guard.
let emptyTranscriptProbeStarted = false;

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

/**
 * Backstop trigger for agents whose transcript has no events at all.
 *
 * The modal is normally reactive: it opens when an assistant turn tagged
 * `is_auth_error` arrives (live over SSE, or the snapshot walk-back). But an
 * initial chat agent whose `/welcome` was never delivered -- claude sat at
 * its no-credentials login screen, so the bootstrap's create aborted before
 * any send -- has an EMPTY transcript. No assistant turn exists and none
 * will ever arrive, so the reactive trigger starves: the panel shows "No
 * events yet for this agent" and the user is never offered a sign-in. For
 * exactly that state, probe auth status directly (the otherwise-unused
 * GET /api/claude-auth/status) and open the modal when logged out. After
 * sign-in the auth-success chokepoint resends `/welcome`, unwinding the
 * whole deadlock.
 */
export function openLoginModalForEmptyTranscript(): void {
  if (emptyTranscriptProbeStarted) return;
  emptyTranscriptProbeStarted = true;
  void m
    .request<{ logged_in?: boolean }>({ method: "GET", url: apiUrl("/api/claude-auth/status") })
    .then((status) => {
      if (status?.logged_in === false) {
        openLoginModal();
      }
    })
    .catch(() => {
      // Best-effort: if the probe fails, the reactive is_auth_error path still stands.
    });
}
