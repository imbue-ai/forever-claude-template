# Canonized Skill-Construction Process

## Overview

- Establish a closed-loop system where the main agent can crystallize ad-hoc work into reusable deterministic skills, heal them when they break, and update them when they fall short — all driven by prompt-based skills plus one Stop-hook.
- Focus on single-unit tasks with >=5 non-read tool calls — any tool call except pure reads (`Read`, `Grep`, `Glob`) counts toward the threshold. The hook is intentionally dumb for v1; the main agent's judgment filters out false positives (e.g. pure-research turns that happened to use many Bash calls).
- Crystallized artifacts are a PEP 723 `uv run --script` Python file (deterministic work, `argparse` interface) placed under the skill's `scripts/` directory, plus a companion `SKILL.md` (discovery + usage). Skills live alongside hand-authored skills in `.agents/skills/<name>/`.
- All skills in this project — crystallized and hand-authored — follow the [agentskills.io](https://agentskills.io/specification) spec: YAML frontmatter with `name` (1-64 chars, lowercase + hyphens, matches parent dir) and `description` (1-1024 chars) required, plus optional `metadata`/`compatibility`/`license`/`allowed-tools`; executable code in `scripts/`, supporting docs in `references/`, static resources in `assets/`; SKILL.md body kept under ~500 lines via progressive disclosure.
- Three new main-agent-invoked skills (`crystallize-task`, `heal-skill`, `update-skill`) do orchestration by spawning workers via the existing `launch-task` flow; the worker-side sub-skills that drive the build are bundled inside `crystallize-task/assets/worker-skills/` and installed into the worker's active `.agents/skills/` via a pre-provision step in the `crystallize-worker` create template (no main-agent staging step is needed since the worker's worktree already contains the `assets/` tree).
- Two user-facing confirmation gates per crystallization (outline, final artifact); workers ask each question by simply ending their turn with it — the user's reply via `mngr message <worker>` naturally resumes them.
- No new review sub-agent: reuse `imbue-code-guardian` (autofix + verify-architecture) on the worker's commits.
- The repo now supports multiple agent runtimes (Claude Code + Hermes). The Stop-hook detection and transcript extraction are Claude Code-specific (`.claude/settings.json` hooks, `$CLAUDE_TRANSCRIPT_PATH`). Hermes would need an equivalent plugin in `agents/hermes/plugins/`. For v1, the crystallization system targets Claude Code; hermes support is a future follow-up. Skills themselves are runtime-neutral (both runtimes discover them from `.agents/skills/`).

## Expected Behavior

### Detection of crystallization candidates

- After every main-agent turn, a Stop-hook inspects the session transcript JSONL and counts tool_use blocks since the last user message.
- All tool calls count *except* pure reads: `Read`, `Grep`, `Glob` are excluded. Everything else — `Bash`, `Edit`, `Write`, `WebFetch`, `WebSearch`, MCP tools, `Skill`, etc. — counts. This is intentionally broad; the main agent applies its own judgment about whether the turn is actually worth crystallizing.
- If the count is >=5, the hook emits a reminder (via stderr with exit code 2, per Claude Code Stop-hook contract) suggesting the main agent consider invoking `crystallize-task`.
- The hook skips entirely when the turn already successfully invoked a skill that has `metadata.crystallized: true` in its `SKILL.md` frontmatter — detected by scanning the Skill tool-use blocks in the turn, resolving each to its SKILL.md, and parsing the YAML `metadata` map.
- The hook skips when running inside a worker sub-agent (detected via an env var set by the worker create template, e.g. `MNGR_AGENT_ROLE=worker`).

### Creation flow

- Main agent, on seeing the hook reminder, decides whether the work is worth crystallizing using the guidance in `crystallize-task/SKILL.md`: the task is a cohesive single unit, is likely to recur, and has a mostly deterministic process.
- If yes, main sends the user a single-line pre-gate question ("I noticed we just did X — worth crystallizing into a reusable skill?") through the deployment's user chat channel.
- On user Yes, main invokes `crystallize-task`, which:
  1. Extracts the just-completed turn from the live session JSONL into `runtime/crystallize/<task-name>/turn.jsonl`.
  2. Spawns a worker via `launch-task` (using a dedicated `crystallize-worker` template that inherits from `worker`), passing the transcript path and a task description. The template's pre-provision step copies `.agents/skills/crystallize-task/assets/worker-skills/*` into the worker's active `.agents/skills/`, so no main-agent staging step is required.
- Worker replicates the task against the transcript, researches APIs / existing utilities as needed, drafts an outline (skill name, inputs, outputs, step-by-step flow, identified edge cases) and proposes a kebab-case skill name the user can override.
- **Gate 1**: worker ends its turn with the outline and the question "approve this outline?"; user replies in the worker's chat. On approval, worker continues; on rejection/changes, worker iterates.
- Worker builds the script (`scripts/run.py`) and `SKILL.md`, hand-crafts 2-3 scenarios (happy path + edge cases), and runs each scenario by invoking the script and inspecting output.
- `imbue-code-guardian` autofix + verify-architecture run on the worker's commits as usual.
- **Gate 2**: worker ends its turn with a summary of the finished artifact and the question "approve and save?"; user replies.
- On approval, worker commits on its `mngr/<task-name>` branch; main agent then merges that branch into its working branch so the skill becomes discoverable locally.

### Run flow

- Main agent discovers applicable skills through the normal SKILL.md-description injection (Claude Code surfaces skills to the model via system reminders).
- When a skill is used, main invokes it through the standard Skill tool / `uv run` invocation documented in its SKILL.md.
- If something goes wrong (script error, wrong output, missing capability), main works around it in-the-moment using ad-hoc tools to still deliver the user's result — does not try to fix the skill inline.
- At turn end (or immediately after the workaround), main performs the reflection described in AGENTS.md and spawns heal or update accordingly.

### Heal flow

- **Trigger**: errors or issues that prevented the existing skill from getting the correct result — i.e. "it should have worked but didn't."
- Main invokes `heal-skill`, which packages the incident transcript + the failing skill's path + a description of what went wrong into a worker task.
- Heal worker replicates the problem, identifies the root cause, applies a fix to `scripts/run.py` and/or `SKILL.md`, re-runs fresh 2-3 scenarios against the fixed script.
- Heal worker goes through Gate 2 (final artifact) only — no outline gate for a fix.
- Commit + merge back to main's branch as in creation.

### Update flow

- **Trigger**: turn-end reflection — "did I have to do additional *deterministic* processing to adapt what the script did to fully complete the user's request?" If yes, main spawns update.
- Update worker reads the current skill + incident transcript and decides between *update-in-place* (add a parameter, add a branch) and *create-new-skill* (the ask is orthogonal enough that a new skill is cleaner).
- Worker runs its own Gate 1 (outline for the update/new skill) and Gate 2 (final artifact). Commits + merges back.

### Skill eligibility for heal/update

- All skills in `.agents/skills/` are eligible — both crystallized and hand-authored built-ins (e.g. `launch-task`, `send-telegram-message`).
- Heal/update of a built-in skill will diverge from the upstream template until manually reconciled via `update-self` (pull) or pushed back via `submit-upstream-changes`; this is accepted for now.

### Turn-end reflection guidance in AGENTS.md

- A new section "Using crystallized skills" is added to AGENTS.md:
  - Prefer an applicable skill over reinventing.
  - If a skill is invoked and errors or delivers an incomplete/incorrect result for the request, invoke `heal-skill` at turn end.
  - After a successful skill invocation, reflect: did you do additional deterministic post-processing the skill could have done itself? If yes, invoke `update-skill`.
  - Both heal and update are non-blocking — they just spawn workers; the user's immediate request is already delivered.

### Worker <-> user communication

- Workers communicate with the user using the same deployment-level mechanism the main agent uses (whatever mechanism is configured for the deployment).
- Gate questions are posed by ending the worker's turn with a direct question; the user's reply, routed via `mngr message <worker>`, naturally resumes the worker. No persistent wait loop is required.

### Destructive-action safety

- No special guard at this layer — Gate 2 approval is considered sufficient consent for subsequent runs.
- Fine-grained safety (`destructive: true` flag, dry-run modes, per-invocation confirmation) is explicitly deferred.

## Changes

### Stop-hook detection (new, Claude Code-specific)

- New script `scripts/detect_crystallization_candidate.sh` (or `.py`) invoked by a new Stop-hook entry in `.claude/settings.json`, alongside the existing `check_repo_root.sh` hook.
- Reads the Stop-hook JSON payload on stdin to get `transcript_path`, parses the JSONL, walks backward from the end to find the most recent user message, counts non-read tool_use blocks (excludes `Read`, `Grep`, `Glob`) between there and end-of-turn.
- Skips when `MNGR_AGENT_ROLE` env var indicates a sub-agent, or when a crystallized skill was successfully invoked in the turn.
- On >=5 qualifying calls, writes a reminder line to stderr and exits with code 2 so the reminder surfaces to the agent on the next turn.
- Hermes equivalent: a future hermes plugin under `agents/hermes/plugins/` that performs the same detection. The detection logic should be factored into a shared Python script in `scripts/` that both the Claude hook and hermes plugin can call.

### New main-agent skills

- `.agents/skills/crystallize-task/SKILL.md` — when-to-use, how-to-decide ("worth it?"), transcript extraction, worker-skill-installation, `launch-task` invocation with the `crystallize-worker` template.
- `.agents/skills/heal-skill/SKILL.md` — same pattern, but for packaging an incident + failing skill into a heal worker task.
- `.agents/skills/update-skill/SKILL.md` — same pattern, for packaging a diverged-behavior incident into an update worker task. Documents the update-in-place vs. new-skill decision rubric so the worker has it.
- Supporting scripts live under each skill's `scripts/` (e.g. `crystallize-task/scripts/extract_turn.py` for pulling the latest turn out of the session JSONL).

### Worker-side bundled sub-skills

- `.agents/skills/crystallize-task/assets/worker-skills/` — skill tree installed into the worker's worktree at spawn-time. The `assets/` location keeps bundled data distinct from scripts the main agent itself would run. Because the worker provisions from a worktree of the same repo, `assets/worker-skills/` is already present in the worker's filesystem, so a single pre-provision step (`install_worker_skills.sh .agents/skills`) copies the tree into the worker's active `.agents/skills/` and no cross-agent staging directory is needed. The installer is still parameterised, so a staging flow can be reintroduced later without code changes. Likely contents:
  - `crystallize-task-worker/` — orchestrates replicate -> outline -> Gate 1 -> build -> scenarios -> review -> Gate 2 -> commit.
  - `heal-skill-worker/` — replicate -> diagnose -> fix -> re-run scenarios -> Gate 2.
  - `update-skill-worker/` — replicate -> decide update-vs-new -> outline -> Gate 1 -> build -> scenarios -> Gate 2.
- Each sub-skill is itself agentskills.io-compliant (SKILL.md + its own `scripts/` for shared helpers like a scenarios-runner and gate-question helper).

### Crystallized-skill marker + layout conventions

- Each crystallized skill conforms to the agentskills.io spec:
  - `.agents/skills/<name>/SKILL.md` — frontmatter has required `name` (matching parent dir, lowercase + hyphens, 1-64 chars) and `description` (<=1024 chars, must describe what the skill does *and* when to use it so the main agent can match it). `metadata:` map carries `crystallized: true`.
  - `.agents/skills/<name>/scripts/run.py` — PEP 723 script with inline deps and `argparse` entry. Scripts are stateless by default; persisted-state location is a future follow-up if needed.
  - Long auxiliary docs or usage examples (if any) go in `.agents/skills/<name>/references/*.md`; static resources (templates, sample inputs) in `assets/`.
  - SKILL.md body stays under ~500 lines; long content is split into referenced files (progressive disclosure).
  - SKILL.md describes usage (example `uv run .agents/skills/<name>/scripts/run.py ...`), what the skill is for, inputs/outputs, and any destructive-action warning prose.
- Scenarios are ephemeral — executed during the build and referenced from the worker's transcript; not persisted on disk.
- Skill-name validation: kebab-case, 1-64 chars, lowercase letters + digits + single hyphens, no leading/trailing hyphens, no `--`. The worker must validate its proposed name against these rules before committing.

### New mngr create template

- Add `crystallize-worker` template under `.mngr/settings.toml` inheriting from `worker`, with:
  - Env var `MNGR_AGENT_ROLE=worker` so the Stop-hook skips inside the worker.
  - `extra_provision_command = ["bash .agents/skills/crystallize-task/scripts/install_worker_skills.sh .agents/skills"]` — copies `assets/worker-skills/*` (already present in the worker's worktree) into its active `.agents/skills/`.
  - Same code-guardian env settings as `worker`.

### Branch/merge convention

- Workers commit to `mngr/<task-name>` as `launch-task` already dictates.
- Main agent, on user Gate 2 approval, merges the worker's branch into its own working branch (fast-forward if possible, merge commit otherwise) so the new/updated skill lands locally.
- Heal/update follow the same merge pattern.

### AGENTS.md additions

- AGENTS.md is the canonical instructions file (CLAUDE.md is a symlink to it). New "Using crystallized skills" section covering: prefer existing skills, heal-on-failure, update-on-divergence, reflection cadence. Kept concise to not bloat the prompt. Written in agent-neutral language since both Claude and Hermes read it.

### Documentation updates

- Update the repo-root README to list the new skills and the crystallization lifecycle at a glance.
- Note in README that crystallization detection is Claude Code-specific for v1; hermes support planned.

### Open defaults (applied because user did not answer)

- Crystallized-vs-built-in marker: `metadata.crystallized: true` inside SKILL.md frontmatter (per agentskills.io — custom flags live in the `metadata` map, not at the top level).
- Commit strategy: worker branch + main-agent merge (matches existing `launch-task` pattern, no template changes needed for branch creation).
- Skill naming: worker proposes a kebab-case name during Gate 1 outline (validated against the agentskills.io `name` rules: 1-64 chars, lowercase + digits + single hyphens, no leading/trailing hyphens, no `--`, matches the skill's parent directory name); user can override inline.
- These can be revisited during refinement.
