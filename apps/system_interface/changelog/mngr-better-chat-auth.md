Workspace Claude auth moved off mngr host env vars, with the in-UI sign-in modal as the sole auth surface. Managed credentials (API key, Imbue key, long-lived token) live in the env block of the shared `CLAUDE_CONFIG_DIR/settings.json`; the primary subscription sign-in uses claude's own credential store and needs no agent restart at all.

- The create templates no longer forward `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` via `pass_host_env`; managed credentials are written only by the system_interface backend into the settings env block (fully controlled: switching modes deletes the other mode's keys, so a stale credential can never shadow the new one).

- The primary "Sign in with your Claude subscription" drives `claude auth login --claudeai` through a backend PTY session: open the sign-in page, approve, paste the code shown (the flow also completes on its own if the CLI's polling gets there first). The credential is stored by the CLI and re-read by running claudes on their next API call, so a fresh workspace signs in with no restart and the welcome message lands immediately. Because managed settings-env keys outrank that credential, choosing subscription while a key/token is active clears the managed keys and restarts the claude agents (shown as a step checklist).

- Sign-in options, in order: Claude subscription (primary), Sign in with Imbue, Use an API key, Get a long-lived token (`claude setup-token`, a 1-year `CLAUDE_CODE_OAUTH_TOKEN` in the settings env, with a subtle "already have a token" paste affordance), and Anthropic Console (API billing) -- Console's key lands in `.claude.json`, so it always clears managed keys and restarts agents.

- "Sign in with Imbue": a link to the Minds desktop app's key-mint page (keyed by this workspace's host id; remote access pops an alert to use the desktop app) plus a textarea for the copied `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` env-style blob. All paste paths share one strict endpoint that rejects unmanaged keys and mixed-mode pastes.

- Credential changes that need an agent restart run it in the background and return immediately; the modal renders live progress as a step checklist ("Restarting agents" / "Resuming your agent", with switch-flavored lead steps) driven by the status endpoint. There is no pre-restart credential probe: a bad credential surfaces through the existing transcript auth-error detection reopening the modal.

- Auth-change restarts cover every claude-binary agent (`claude` AND `worker` types; previously workers were silently missed) via fused `mngr start --restart` calls, and previously-RUNNING agents get a "credentials updated, please continue" resume message so unattended work resumes. The `main` services agent is never touched.

- A persistent "Agent auth" entry below the chat (next to "Open agent terminal") opens the modal any time, with a muted header showing how the workspace is currently signed in (including the account email for browser sign-ins). A page-load status check pops the modal on a freshly created (never signed-in) workspace, making sign-in the designed first-boot step.

- Typing `/logout` into the chat is intercepted with a dialog pointing at the agent-auth screen instead of being delivered to the agent's TUI, where it would exit the agent's process and wipe shared onboarding state without cleanly signing the workspace out.

- LiteLLM budget/auth rejection patterns were added to the transcript auth-error detection, so an exhausted daily budget also surfaces the modal.

- The `use-ai-integration` skill's keyed path now resolves credentials from the shared settings at call time (`read_workspace_ai_credentials()` in `claude_p.py`) instead of `os.environ`, so services pick up auth changes without restarts.

- **MIGRATION (existing workspaces):** run `uv run python scripts/migrate_claude_auth.py` from the repo root (from the workspace terminal or an agent -- the restart phase runs detached, so an agent invoking it on itself still completes). It moves any host-env Claude credentials into the settings env block, scrubs them from `$MNGR_HOST_DIR/env`, and restarts claude agents. Subscription-based workspaces need no migration.
