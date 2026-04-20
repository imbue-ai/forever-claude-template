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
- `.mngr/settings.toml` - Agent types, create templates, command defaults.
- `.claude/` - Claude Code-specific config (settings, hooks, plugins). Ignored by hermes.
- `.agents/skills/` - Shared skills (telegram, task delegation, services, self-update). Hermes loads them via `skills.external_dirs` in its config; Claude Code discovers them via a symlink under `.claude/skills/`.
- `agents/` - Per-runtime layer resolved at provisioning time. `agents/hermes/` contains hermes' `config.yaml` overrides, plugin hooks, and a `setup.sh` that merges the overrides on top of the user's `~/.hermes/config.yaml`.
- `scripts/` - Shared hook behaviours and utility scripts
- `events_processor/` - Pre-configured directory for creating persistent sub-agents.
- `services.toml` - Background services managed by bootstrap.
- `libs/telegram_bot/` - Telegram bot, send CLI, and history viewer.
- `libs/bootstrap/` - Service manager (reconciles services.toml with tmux windows).
- `vendor/mngr/` - A vendored, mutable copy of mngr. Note that making changes here *will* affect the behavior of the `mngr` command.
- `vendor/tk/` - A vendored copy of the [tk](https://github.com/wedow/ticket) ticket tracker. The `ticket` script (also callable as `tk`) manages tickets stored as markdown in `.tickets/` (gitignored)

## Create templates

- `main` - Top-level Claude agent.
- `hermes_main` - Top-level Hermes agent. Runs `agents/hermes/setup.sh` via `extra_provision_command` to overlay template config onto `HERMES_HOME`.
- `dev` / `docker` / `lima` / `vultr` - Mode templates (agent-agnostic). Compose with `main` or `hermes_main`.
- `chat` / `worktree` - Sub-agent templates for additional sessions.
- `worker` - Sub-agent for delegated tasks via the `launch-task` skill (inherits from Claude; includes code review).
- `crystallize-worker` - Sub-agent for the skill crystallization / heal / update lifecycle. Inherits from `worker` and pre-installs the bundled `crystallize-task-worker`, `heal-skill-worker`, and `update-skill-worker` sub-skills into its own `.agents/skills/`.

## Skill crystallization lifecycle

The main agent can promote ad-hoc work into reusable deterministic skills, heal skills that fail, and extend skills that came up short. The user-invokable surface is three skills (main agent side):

- `crystallize-task` - Turn the just-finished turn into a new skill. Triggered by a Stop-hook reminder when the turn used >=5 non-read tool calls (detection lives in `scripts/detect_crystallization_candidate.py`).
- `heal-skill` - Repair a skill that errored or produced wrong results.
- `update-skill` - Extend a skill (or split off a new sibling) when post-processing revealed a gap.

Each of these spawns a `crystallize-worker` sub-agent that runs a matching build / heal / update sub-skill bundled under `.agents/skills/crystallize-task/assets/worker-skills/`. Workers commit to `mngr/<task-name>` branches; main merges on user approval.

Crystallized skills are marked with `metadata.crystallized: true` in their SKILL.md frontmatter and follow the [agentskills.io](https://agentskills.io/specification) layout (`scripts/run.py` as a PEP 723 script, companion SKILL.md, optional `references/` and `assets/`).

Detection is Claude Code-specific for v1 (the Stop hook lives in `.claude/settings.json`). Hermes parity would come via an equivalent plugin under `agents/hermes/plugins/`; the detection logic in `scripts/detect_crystallization_candidate.py` is reusable as-is.

## Hermes notes

- The template does not pin a hermes model. Whatever `~/.hermes/config.yaml` specifies wins. The template only overrides `platform_toolsets.cli` (curated toolset) and `skills.external_dirs` (to expose `.agents/skills/`).
- Hermes plugin hooks are fire-and-forget, so the shared guard scripts print warnings but cannot actually block a tool call the way Claude Code's `exit 2` PreToolUse hook does.
- The `imbue-code-guardian` marketplace plugin is Claude Code-specific. Hermes agents do not get the `/autofix` / `/verify-conversation` / `/review` workflow.
