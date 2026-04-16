# forever-claude-template

A self-contained template for running a persistent agent that communicates via Telegram, delegates work to sub-agents, and can manage its own background services. Supports either [Claude Code](https://claude.com/claude-code) or [Hermes](https://github.com/NousResearch/hermes-agent) as the agent runtime. The repo name is a historical artifact.

## Usage

Claude agent:

```bash
mngr create my-workspace main -t local \
    --host-env MINDS_WORKSPACE_NAME=my-workspace \
    --project ~/project/forever-claude-template \
    --pass-env TELEGRAM_BOT_TOKEN \
    --pass-env TELEGRAM_USER_NAME
```

Hermes agent: swap `main` for `hermes_main`.

## Structure

- `AGENTS.md` - Agent instructions (canonical). `CLAUDE.md` is a symlink to this file so Claude Code and any tool that expects `CLAUDE.md` still works; edits to either filename hit the same bytes.
- `parent.toml` - Upstream repo for pulling updates.
- `.mngr/settings.toml` - Agent types (`main`, `worker`, `hermes_main`, …), create templates, command defaults.
- `.claude/` - Claude Code-specific config (settings, hooks, plugins). Ignored by hermes.
- `.agents/skills/` - Shared skills (telegram, task delegation, services, self-update). Hermes loads them via `skills.external_dirs` in its config; Claude Code discovers them via a symlink under `.claude/skills/`.
- `agents/` - Per-runtime layer resolved at provisioning time. `agents/hermes/` contains hermes' `config.yaml` overrides, plugin hooks, and a `setup.sh` that merges the overrides on top of the user's `~/.hermes/config.yaml`.
- `scripts/` - Shared hook behaviours invoked from both runtimes: `agent_setup.sh` (SessionStart uv sync), `guard_commit_rewrite.sh` (blocks `git rebase` / `--amend`), `check_repo_root.sh` (reminds the agent to end in the repo root). `claude_update_plugin.sh` and `claude_status_line.sh` are Claude-only. `create_reviewer_settings.sh` writes the `imbue-code-guardian` config, which is a Claude Code marketplace plugin and does not apply to hermes.
- `events_processor/` - Pre-configured directory for creating persistent sub-agents. Relies on Claude Code's stop-hook exit-code-2 loop; deferred for hermes.
- `services.toml` - Background services managed by bootstrap.
- `libs/telegram_bot/` - Telegram bot, send CLI, and history viewer.
- `libs/bootstrap/` - Service manager (reconciles services.toml with tmux windows).
- `vendor/mngr/` - A vendored, mutable copy of mngr. Note that making changes here *will* affect the behavior of the `mngr` command.

## Create templates

- `main` - Top-level Claude agent.
- `hermes_main` - Top-level Hermes agent. Runs `agents/hermes/setup.sh` via `extra_provision_command` to overlay template config onto `HERMES_HOME`.
- `dev` / `docker` / `lima` / `vultr` - Mode templates (agent-agnostic). Compose with `main` or `hermes_main`.
- `chat` / `worktree` - Sub-agent templates for additional sessions.
- `worker` - Sub-agent for delegated tasks via the `launch-task` skill (inherits from Claude; includes code review).

## Hermes notes

- The template does not pin a hermes model. Whatever `~/.hermes/config.yaml` specifies wins. The template only overrides `platform_toolsets.cli` (curated toolset) and `skills.external_dirs` (to expose `.agents/skills/`).
- Hermes plugin hooks are fire-and-forget, so the shared guard scripts print warnings but cannot actually block a tool call the way Claude Code's `exit 2` PreToolUse hook does.
- The `imbue-code-guardian` marketplace plugin is Claude Code-specific. Hermes agents do not get the `/autofix` / `/verify-conversation` / `/review` workflow.
