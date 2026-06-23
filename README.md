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
- `supervisord.conf` - Supervisord config defining the background services
- `libs/telegram_bot/` - Telegram bot, send CLI, and history viewer
- `libs/bootstrap/` - First-boot setup, then launches supervisord to supervise the services
- `vendor/mngr/` - A vendored, mutable copy of mngr, synced in from the mngr monorepo as a plain snapshot (via `git archive` for releases, `rsync` for dev iteration -- not a git subtree or submodule). Note that making changes here *will* affect the behavior of the `mngr` command.
- `vendor/tk/` - A vendored, manually-maintained fork of the [tk](https://github.com/wedow/ticket) ticket tracker (upgraded by hand; we don't pull from upstream). The `ticket` script (also callable as `tk`) manages tickets stored as markdown. We point `TICKETS_DIR` at `runtime/tickets/` (set in `.mngr/settings.toml`'s `host_env`) so tickets are backed up alongside the rest of `runtime/` on the `mindsbackup/$MNGR_AGENT_ID` branch.

## Create templates

- `worker` - For sub-agents created via the launch-task skill (includes code review)
- `subskill-worker` - Sub-agent for any flow that hands its worker a bundled sub-skill (the skill crystallization / heal / update lifecycle and the update-system-interface flow). Inherits from `worker` and pre-installs every parent skill's bundled `<parent>-worker` sub-skill (auto-discovered from each `assets/worker/` directory) into its own `.agents/skills/`.

## Skill crystallization lifecycle

The main agent can promote ad-hoc work into reusable deterministic skills, heal skills that fail, and extend skills that came up short. The user-invokable surface is three skills (main agent side):

- `crystallize-task` - Turn the just-finished turn into a new skill. Triggered by a Stop-hook reminder when the turn used >=8 non-read tool calls (detection lives in `scripts/detect_crystallization_candidate.py`).
- `heal-skill` - Repair a skill that errored or produced wrong results.
- `update-skill` - Extend a skill (or split off a new sibling) when post-processing revealed a gap.

Each of these spawns a `subskill-worker` sub-agent that runs a matching build / heal / update sub-skill bundled under each parent skill's `assets/worker/` directory (`.agents/skills/crystallize-task/assets/worker/`, `.agents/skills/heal-skill/assets/worker/`, `.agents/skills/update-skill/assets/worker/`). Workers commit to `mngr/<task-name>` branches; main merges on user approval. (The same template also backs the `update-system-interface` flow, whose worker sub-skill is bundled at `.agents/skills/update-system-interface/assets/worker/`.)

Crystallized skills are marked with `metadata.crystallized: true` in their SKILL.md frontmatter and follow the [agentskills.io](https://agentskills.io/specification) layout (`scripts/run.py` as a PEP 723 script, companion SKILL.md, optional `references/` and `assets/`).