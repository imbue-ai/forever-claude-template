# In-UI Claude login flow for unauthenticated minds

## Refined prompt

> In a mind created from this template, Claude's credential sync from the user's host doesn't always work (especially for subscription auth). When Claude ends up unauthenticated, the only recovery today is to open a ttyd terminal in the mind, run `/login`, paste the URL into a browser, copy the code, and paste it back — buggy and cumbersome. Lift this into a system-interface modal.
>
> * **Detection (load-time):** On every page load, check Claude auth state via `claude auth status --json`. While unauthenticated, the modal pops up automatically each load. Once authenticated, only re-check on a totally fresh page load.
> * **Detection (reactive):** Extend the existing system-interface JSONL transcript parser to pattern-match assistant message text against known Claude auth-error signatures, tag the resulting event with an `is_auth_error: bool` flag, and have the frontend pop the modal when it sees a tagged event over the existing SSE stream.
> * **Scope:** A single mind per system-interface instance.
> * **Modal layout:** Three provider options mirroring the outer minds creation form's `AIProvider` enum — **Claude subscription** (default), **Anthropic Console**, and **API key** (raw `sk-ant-...`).
>   * Subscription / Console: drive `claude auth login --claudeai` or `claude auth login --console` via a PTY subprocess. Parse the printed `claude.ai/oauth/authorize` URL out of stdout, surface it to the modal, accept the user's pasted `CODE#STATE`, write it to the subprocess's stdin.
>   * API key: write `ANTHROPIC_API_KEY=<key>` into the mind's host env file (the same one bootstrap extends for `CLAUDE_CONFIG_DIR`), then restart the chat agent's `claude` session so the new env is in effect immediately.
> * **Open-in-browser:** Modal uses a `target="_blank"` link to the Anthropic OAuth URL. For now, accept that this opens inside the Electron WebContentsView. The user will wire `shell.openExternal` separately in `~/utilities/mngr/apps/minds/electron/main.js` as follow-up work.
> * **Background poll:** While the modal is open, the backend polls `claude auth status --json` every ~3s; auto-closes the modal on detected success.
> * **Modal style:** Blocking modal on first auto-detect per page load. If the user dismisses, replace with a non-blocking banner that re-opens the modal on click.
> * **Mount point:** Modal/banner live inside the chat panel only — sidebar remains interactive.
> * **Success state:** Modal shows "Signed in as user@example.com — subscription: Max" with a "Done" button to dismiss.
> * **Welcome-resend (on any auth-success path):** After auth success, inspect the chat agent's pane via the existing `GET /api/agents/{agentId}/screen` endpoint. If the pane does not contain the verbatim opening line of the welcome skill (read at runtime from `.agents/skills/welcome/SKILL.md` so the two stay in sync), re-send `/welcome` to the chat agent using the same internal Python machinery the bootstrap uses (not a `mngr message` subprocess).
> * **Naming:** New endpoints use `/api/claude-auth/...` to avoid collision with the existing `/login` and `/welcome` HTTP routes that the system-interface backend uses for its own session auth.
> * **Auth-error pattern list:** Initial set based on the official `code.claude.com/docs/en/errors` reference: `Not logged in [·-] Please run /login`, `Invalid API key`, `OAuth token (revoked|has expired|does not meet scope requirement)`, `"type"\s*:\s*"authentication_error"`, `API Error:\s*401\b`, `Invalid authentication credentials`, `Credit balance is too low`, `organization has been disabled`. Patterns live in a dedicated constants module so they're easy to extend.
> * **Testing:** Unit tests for the new transcript-parser auth-error detection, the welcome-text detection helper, and the modal component. One integration test that mocks the `claude auth login` PTY end-to-end and asserts the credentials-file write plus the welcome-resend dispatch. No e2e/acceptance test gating CI for the OAuth flow itself, since the user-facing browser leg can't be automated meaningfully.

## Overview

- **Goal:** Replace the manual ttyd-terminal `claude /login` paste flow with a system-interface modal so users can recover from broken Claude auth without dropping into a shell.
- **Detection happens in two places:** a load-time `claude auth status --json` check, and a reactive signal added to the transcript-parsing pipeline that already streams agent output to the UI.
- **Three sign-in modes** mirror the outer minds creation form: subscription, Console, raw API key. Subscription and Console drive `claude auth login --claudeai`/`--console` via a PTY subprocess. API key writes `ANTHROPIC_API_KEY` into the bootstrap-managed host env file and restarts the chat agent.
- **Welcome resend on auth success:** if the chat agent's pane is missing the welcome skill's opening line, re-dispatch `/welcome` via the same internal Python machinery the bootstrap already uses.
- **Electron `shell.openExternal` plumbing is out of scope** for this change. The OAuth URL opens via a plain `target="_blank"` link; the user will route it externally in a follow-up to the outer minds Electron app.

## Expected behavior

- On every system-interface page load, the chat panel calls a new `/api/claude-auth/status` endpoint. If `loggedIn: false`, the modal opens immediately, blocking interaction with the chat panel only (sidebar stays usable).
- Once logged in, no further auth checks fire until the next fresh page load — the modal does not re-poll while the user is on the page.
- When Claude emits an auth error mid-session, the corresponding assistant message in the SSE stream arrives tagged with `is_auth_error: true`. The chat panel reacts by opening the modal even if the page wasn't reloaded.
- If the user dismisses the modal without signing in, a non-blocking banner appears at the top of the chat panel ("Claude isn't signed in"). Clicking the banner re-opens the modal.
- In the modal, the user picks one of three providers: **Claude subscription** (default), **Anthropic Console**, or **API key**.
  - For subscription / Console, the modal shows a button that opens the Anthropic OAuth URL in a new tab via `target="_blank"`. After the user authenticates externally and copies the displayed code, they paste `CODE#STATE` into a text field in the modal and click "Verify".
  - For API key, the user pastes a `sk-ant-...` value into a password field and clicks "Save".
- While the modal is open, the backend polls `claude auth status --json` every ~3s. If the user authenticates by some other route (e.g. running `claude` directly in a terminal), the modal auto-closes.
- On any successful auth, the modal briefly displays "Signed in as user@example.com — subscription: Max" (or similar from `claude auth status`) and waits for the user to click "Done" before dismissing.
- After dismissal, the backend inspects the chat agent's tmux pane via the existing `/api/agents/{agentId}/screen` endpoint. If the pane does not contain the welcome skill's verbatim opening line, the backend re-sends `/welcome` to the chat agent. The user sees the welcome message appear in the chat. Welcome-resend fires on all auth-success paths (paste, poll-detected, banner-triggered).
- All new backend endpoints live under `/api/claude-auth/...` to avoid collision with the existing `/login` and `/welcome` HTTP routes used for system-interface session auth.

## Changes

### Backend (system_interface)

- New `claude_auth` module under `apps/system_interface/imbue/minds_workspace_server/` handling:
  - Auth status check (`claude auth status --json` parsing).
  - PTY-driven `claude auth login --claudeai` / `--console` subprocess: parse the printed OAuth URL, accept the user's pasted code, complete the flow.
  - API-key write: append `ANTHROPIC_API_KEY=<key>` to the mind's host env file (same one bootstrap extends at boot for `CLAUDE_CONFIG_DIR`) and restart the chat agent's `claude` session via the existing mngr-internal restart path.
- New HTTP/SSE endpoints under `/api/claude-auth/...`:
  - `GET /status` → returns the parsed `claude auth status --json` result.
  - `POST /start` → begins a login attempt for a chosen provider; returns a session token plus, for OAuth modes, the OAuth URL extracted from stdout.
  - `POST /submit-code` → submits the user's pasted `CODE#STATE` to the running PTY subprocess.
  - `POST /submit-api-key` → persists the API key into the host env file and restarts the chat agent.
  - `GET /poll` → returns the live auth state while the modal is open (the modal polls this every ~3s instead of hitting `/status` directly, so backend can also notice success via the underlying subprocess completing).
- Extend `apps/system_interface/imbue/minds_workspace_server/session_parser.py` to pattern-match assistant message text against the agreed auth-error regex set. When matched, set a new `is_auth_error: bool` field on the emitted `TranscriptEvent`. Patterns live in a new dedicated constants module so the list is easy to update independently of parser logic.
- New helper module for the welcome-text detection plus welcome resend:
  - At call time, read `.agents/skills/welcome/SKILL.md`, parse out the first non-frontmatter line, and use it as the substring to grep for in the chat agent's pane content (so the detector stays automatically in sync with the skill).
  - Pane-content read uses the existing `GET /api/agents/{agentId}/screen` route.
  - On miss, dispatch `/welcome` to the chat agent via the same internal Python machinery (`_send_enter_and_wait` / base_agent helpers) the bootstrap already uses — not a `mngr message` shell-out.
- Wire `claude_auth` and the welcome-resend helper together: every auth-success path (paste-driven, poll-detected, API-key write) goes through one `_on_auth_success` chokepoint that runs the welcome-resend check.

### Frontend (system_interface)

- New `ClaudeLoginModal.ts` Mithril component:
  - Three-way provider radio (Subscription default, Console, API key).
  - For OAuth modes: "Open Anthropic login" button rendering a `target="_blank"` `<a>` to the URL returned by `/api/claude-auth/start`. Below it, a `CODE#STATE` text input and a "Verify" button posting to `/submit-code`. Inline error display.
  - For API key: a password input and a "Save" button posting to `/submit-api-key`.
  - Success state: "Signed in as {email} — subscription: {tier}" plus a "Done" button.
  - While open, polls `/poll` every ~3s and auto-dismisses on detected success.
- New `ClaudeLoginBanner.ts` Mithril component:
  - Non-blocking banner shown at the top of the chat panel after the user dismisses the modal without signing in.
  - Clicking re-opens the modal.
- Wire detection into the chat panel:
  - On chat-panel mount, call `/api/claude-auth/status`. If unauthenticated, open the modal.
  - Subscribe to the existing SSE event stream and, on any event with `is_auth_error: true`, open the modal.
- Both modal and banner mount inside the chat panel (not the dockview root), so the sidebar remains interactive.

### Existing files modified

- `apps/system_interface/imbue/minds_workspace_server/session_parser.py` — emit `is_auth_error` on `TranscriptEvent`.
- `apps/system_interface/imbue/minds_workspace_server/server.py` (or equivalent) — register the new `/api/claude-auth/...` routes.
- `apps/system_interface/frontend/src/views/ChatPanel.ts` — mount modal/banner, wire load-time check + SSE auth-error subscription.
- `apps/system_interface/frontend/src/models/Response.ts` (or wherever `TranscriptEvent` is mirrored client-side) — add `is_auth_error?: boolean`.
- `libs/bootstrap/` — confirm and (if needed) expose the existing host-env-file path as a constant the new `claude_auth` module can reuse for `ANTHROPIC_API_KEY` writes.

### Testing

- Unit tests:
  - `session_parser` auth-error tagging: assert each of the 8 reference patterns produces `is_auth_error: true`; assert non-error assistant text does not.
  - Welcome-text detector: assert it correctly reads the skill file, extracts the first non-frontmatter line, and returns hit/miss against synthetic pane content.
  - Claude-auth status parsing: assert the JSON returned by a real `claude auth status --json` invocation deserializes into the expected shape.
- Integration test (single):
  - Mock the `claude auth login --claudeai` PTY subprocess so it deterministically prints a fake OAuth URL and then accepts a fake `CODE#STATE`.
  - Drive the full `/api/claude-auth/...` flow end-to-end through the FastAPI test client.
  - Assert: credentials file written in expected shape, `_on_auth_success` chokepoint invoked, welcome-resend dispatched.
- No e2e/acceptance test for the real OAuth flow (the user-facing browser leg can't be automated meaningfully). Manual-verification checklist in the PR description instead.

## Open questions

- **Auth-error pattern coverage in practice.** The pattern set comes from the official Claude Code errors documentation, but real-world JSONL transcripts may surface 401s as raw API JSON in tool-result blocks rather than assistant `text` (per behaviour hinted at in `anthropics/claude-code` issues #48656, #5893, #33879). An empirical check against a real expired-token session JSONL before shipping would tighten the recall side.
- **Restart mechanism for the chat agent's `claude` session after an API-key write.** Mngr has internal restart paths but the exact entry point that picks up an updated host env file without dropping tmux state needs to be confirmed before implementation.
- **Banner persistence across reloads.** Spec says modal pops on every load while unauthenticated, banner appears only after user dismisses. Should the banner persist across reloads (i.e. if the user dismissed in the previous session and is still unauthenticated, do we open the modal again or go straight to the banner)? Default assumption is: every fresh load starts with the blocking modal, regardless of prior dismissals — but worth confirming.
- **Welcome-resend race.** If the user dismisses the modal very quickly, the pane scrape may run before the chat agent has had time to print anything at all (since on first boot the agent might still be initializing). The detector would then see an empty pane, treat it as a miss, and re-send `/welcome` — which is actually fine, just worth noting. No retry/backoff is currently planned.
- **Console-mode account semantics.** `claude auth login --console` authenticates against Anthropic Console (API-usage billing) rather than the Claude.ai subscription. The success state's "Signed in as ... — subscription: Max" string assumes subscription tier info is present; for Console accounts that field may be null/absent. The success-state copy needs a fallback.

## Implementation deltas from this plan

The implementation that landed diverges from the original plan in several places. The plan body above is preserved as a historical record; the actual behavior is summarized here:

- **No load-time `/api/claude-auth/status` check.** Detection is purely reactive: on chat-panel mount we scan the existing transcript snapshot for the most recent assistant message and pop the modal only if that message carries `is_auth_error: true`, and we subscribe to the SSE stream so any new auth-error event also pops it. No HTTP status probe is issued at page load.
- **No `/poll` endpoint or background poll loop.** The modal closes only when the user dismisses it (via the close affordance, "Done" after success, or "Close" after an error). External auth-success cannot dismiss it -- but the next assistant message will simply not carry `is_auth_error: true`, so the modal does not re-open.
- **No non-blocking banner.** The modal is the only auth-recovery affordance. If the user dismisses it without signing in, the next reactive auth-error signal re-opens it.
- **Pane reads use `tmux capture-pane` directly.** `welcome_resend._default_capture_agent_pane` shells out to `tmux capture-pane -t <prefix><name>` rather than going through `/api/agents/{agentId}/screen`. This avoids a self-call back through the HTTP layer.
- **OAuth paths do not restart any agents.** Subscription and Console OAuth write `$CLAUDE_CONFIG_DIR/.credentials.json`, which is shared across every running claude in the mind; the next API call picks the new credentials up. Only the raw-API-key path restarts every `type: claude` agent (via `mngr stop` + `mngr start`) because env vars are inherited at process start and cannot be updated in place.
- **Auth-error pre-login turns are hidden in the transcript view.** `computeAuthErrorHiddenEventIds` in `message-renderers.ts` drops every assistant auth-error message that precedes the first successful assistant message, plus the user turn that triggered each. Mid-session token expirations (auth errors after at least one successful exchange) are left visible.
- **OAuth URL regex was loosened.** `_OAUTH_URL_REGEX = r"https://\S*oauth/authorize\S*"` matches any host whose path contains `oauth/authorize`, covering both `claude.com/cai/oauth/authorize?...` (claudeai) and `platform.claude.com/oauth/authorize?...` (console), plus the legacy `claude.ai/...` form.
