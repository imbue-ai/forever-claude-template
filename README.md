# forever-claude-template

A self-contained template for running a persistent Claude agent that communicates via Telegram, delegates work to sub-agents, and can manage its own background services.

## Usage

```bash
mngr create my-workspace main -t local \
    --host-env MINDS_WORKSPACE_NAME=my-workspace \
    --project ~/project/forever-claude-template \
    --pass-env TELEGRAM_BOT_TOKEN \
    --pass-env TELEGRAM_USER_NAME
```

## Structure

- `CLAUDE.md` - Agent instructions
- `parent.toml` - Upstream repo for pulling updates
- `.mngr/settings.toml` - Agent types, create templates, command defaults
- `skills/` - Agent skills (telegram, task delegation, services, self-update)
- `scripts/` - Utility scripts (reviewer settings)
- `event-processor/` - Pre-configured directory for creating persistent sub-agents
- `services.toml` - Background services managed by bootstrap
- `libs/telegram_bot/` - Telegram bot, send CLI, and history viewer
- `libs/bootstrap/` - Service manager (reconciles services.toml with tmux windows)
- `vendor/mngr/` - A vendored, mutable copy of mngr. Note that making changes here *will* affect the behavior of the `mngr` command
- `vendor/tk/` - A vendored copy of the [tk](https://github.com/wedow/ticket) ticket tracker. The `ticket` script (also callable as `tk`) manages tickets stored as markdown in `.tickets/` (gitignored)

## Create templates

- `worker` - For sub-agents created via the launch-task skill (includes code review)
- `crystallize-worker` - Sub-agent for the skill crystallization / heal / update lifecycle. Inherits from `worker` and pre-installs the bundled `crystallize-task-worker`, `heal-skill-worker`, and `update-skill-worker` sub-skills into its own `.agents/skills/`.

## Skill crystallization lifecycle

The main agent can promote ad-hoc work into reusable deterministic skills, heal skills that fail, and extend skills that came up short. The user-invokable surface is three skills (main agent side):

- `crystallize-task` - Turn the just-finished turn into a new skill. Triggered by a Stop-hook reminder when the turn used >=5 non-read tool calls (detection lives in `scripts/detect_crystallization_candidate.py`).
- `heal-skill` - Repair a skill that errored or produced wrong results.
- `update-skill` - Extend a skill (or split off a new sibling) when post-processing revealed a gap.

Each of these spawns a `crystallize-worker` sub-agent that runs a matching build / heal / update sub-skill bundled under `.agents/skills/crystallize-task/assets/worker-skills/`. Workers commit to `mngr/<task-name>` branches; main merges on user approval.

Crystallized skills are marked with `metadata.crystallized: true` in their SKILL.md frontmatter and follow the [agentskills.io](https://agentskills.io/specification) layout (`scripts/run.py` as a PEP 723 script, companion SKILL.md, optional `references/` and `assets/`).
