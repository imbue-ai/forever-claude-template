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
- `vendor/mngr/` - A vendored, mutable copy of mngr. Note that making changes here *will* affect the behavior of the `mngr` command
- `vendor/tk/` - A vendored copy of the [tk](https://github.com/wedow/ticket) ticket tracker. The `ticket` script (also callable as `tk`) manages tickets stored as markdown. We point `TICKETS_DIR` at `runtime/tickets/` (set in `.mngr/settings.toml`'s `host_env`) so tickets are backed up alongside the rest of `runtime/` on the `mindsbackup/$MNGR_AGENT_ID` branch.

## Create templates

- `worker` - For sub-agents created via the launch-task skill (includes code review)
- `subskill-worker` - Sub-agent for any flow that hands its worker the generic harden worker (the crystallize / update / heal artifact lifecycle, including the update-system-interface flow). Inherits from `worker` and pre-installs the single generic worker from `.agents/shared/worker/` into its own `.agents/skills/` as `harden-worker`.

## Artifact harden lifecycle

The main agent can promote ad-hoc work into reusable artifacts, fix artifacts that fail, and extend artifacts that came up short -- across skills, web services, and the system interface. The user-invokable surface is three generic operation leads (main agent side), each parameterized by the artifact:

- `crystallize-artifact` - Create a new artifact (default: a skill reconstructed from the just-finished turn). Invoked directly post-turn, or by the live-half wrappers (`build-web-service`, `fetch-process-show`) once a prototype is confirmed.
- `heal-artifact` - Fix a skill or service that errored or produced wrong results.
- `update-artifact` - Extend / refactor / verify a skill, service, or shared reference; one flow with a committed-vs-emergent design-gate toggle.

Each lead spawns a `subskill-worker` sub-agent that runs the single generic `harden-worker` sub-skill. The worker reads the operation and artifact from its task file and composes the universal `harden-artifact.md` contract with one `op-*.md` and one `artifact-*.md` reference under `.agents/shared/worker/references/`. Workers commit to `mngr/<task-name>` branches; main merges on user approval. (The same template also backs the `update-system-interface` flow, which wraps `update-artifact` with `artifact=system-interface` and adds its preview / safe-reveal go-live.)

Crystallized skills are marked with `metadata.crystallized: true` in their SKILL.md frontmatter and follow the [agentskills.io](https://agentskills.io/specification) layout (`scripts/run.py` as a PEP 723 script, companion SKILL.md, optional `references/` and `assets/`).