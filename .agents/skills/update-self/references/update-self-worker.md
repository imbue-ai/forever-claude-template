# Update-self worker guidance

You are the background worker for a safe update-self pass, in your own worktree
branched off the lead's `HEAD`. Merge the target upstream ref, triage the
conflicts, validate what reconciled, and report a "what's new" summary. **You
never restart a live service or reveal anything** -- you validate in isolation
and report; the lead applies the update.

The deterministic pieces (target resolution, merged-vs-pulled classification,
changelog gathering) live in
`runtime/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py`
-- call it, don't reimplement. That
`runtime/update-self/skill-at-target/.agents/skills/update-self` path is the copy
of the update-self flow shipped with the version being updated to (the lead staged
it and it was synced into this worktree with the runtime dir); running from it
means you use the target version's flow, not this worktree's possibly-stale copy.
Impact analysis (who depends on a changed file) is deliberately *not* scripted;
Step 4a is your recipe for it.

## 1. Resolve inputs

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py 'runtime/update-self/task.md')"
```

Sets `LEAD_AGENT`, `FINISH_REPORT_PATH`, and `TARGET_REF`. Run every
`update_self.py` call below from
`runtime/update-self/skill-at-target/.agents/skills/update-self/scripts/` (a fixed
path -- reference it by literal each time rather than stashing it in a shell
variable, since each bash invocation starts a fresh shell). If the worktree has no
`.venv`, `uv sync --all-packages` once. Ensure the ref is present:

```bash
git fetch upstream --tags
BASE=$(git merge-base HEAD "$TARGET_REF")
```

## 2. Reason about the diff, then trial-merge

Preview the impacted classes and read the upstream diff for genuine
incompatibilities (a settings-schema change the running code can't parse, a
renamed interface a local customization depends on, both sides rewriting the same
region) -- so you can frame a precise `question` before committing to the merge:

```bash
python3 runtime/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local HEAD --target "$TARGET_REF" --base "$BASE"
```

Then enumerate the real conflict set without committing:

```bash
git merge --no-commit --no-ff "$TARGET_REF"
git diff --name-only --diff-filter=U
```

Triage each conflict, first rule that applies wins:

- **Generated lockfiles (`uv.lock`, `package-lock.json`) -> regenerate, never
  side-pick or hand-merge.** If both sides' manifests changed, *neither* side's
  lock matches the merged manifest, and hand-editing conflict markers in a lock
  produces a file the tool can't parse. Resolve the corresponding manifest
  first, then regenerate from it (`uv lock` in the lock's directory; `npm
  install --package-lock-only` for npm) and `git add` the result.
- **Agent-owned files -> keep local** (`PURPOSE.md`, `runtime/`):
  `git checkout --ours -- <path> && git add <path>`.
- **Mixed files (`CLAUDE.md` and similar) -> merge by judgment.** Do not blanket
  keep-local: upstream additions (new sections, updated shared guidance) are
  often worth integrating. Resolve by editing the file -- keep the
  agent-specific customizations, fold in the upstream additions.
- **Files untouched locally -> take upstream**: `git checkout --theirs -- <path>
  && git add <path>`.
- **No clear resolution -> gate, as a last resort.** Only after you have
  genuinely tried to reconcile and found no resolution that preserves both
  sides' intent (both rewrote the same region incompatibly and the answer
  depends on what the user wants) do you write a `name: question` gate (Step 6)
  describing the file, what each side did, and the options; push and stop. The
  lead relays the user's decision; apply it and continue. Never gate on
  anything the rules above or reasonable judgment can settle.

**Lockfiles need attention even without a conflict.** Git will happily
auto-merge two divergent `uv.lock`s into a semantically invalid file (duplicate
`[[package]]` entries uv then can't disambiguate) -- this has bricked a live
workspace before. When the Step 2 `classify-merge` shows a lockfile changed on
**both** sides relative to the base, discard git's auto-merge of it and
regenerate (`uv lock` / `npm install --package-lock-only`) before committing,
even when the merge reported no conflicts. (The repo's `.gitattributes` marks
these locks `merge=binary` so divergence *should* surface as a conflict, but do
not rely on it -- older local histories may predate that.)

No conflicts at all -- after the lockfile check above -- means a clean pull; go
straight to committing.

## 3. Commit with the marker subject

Once every path is resolved and staged, commit with the exact subject (tools like
`assist` classify built-in code by the `update-self:` prefix -- never reword it):

```bash
git commit -m "update-self: merge upstream template ($TARGET_REF)"
```

If a fix needs a new dependency, add it and commit the manifest change so it's in
the merge.

## 4. Classify and validate the merged set

Split what upstream changed into the reconciled **merged** set (validate) vs the
clean **pulled-in** set (trust as upstream-tested):

```bash
python3 runtime/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local HEAD^1 --target "$TARGET_REF"
```

`HEAD^1` is pre-merge local; `HEAD` is the merge. `projects_to_validate`,
`reveal_classes_merged`, and the per-file entries scope the work below.
**Validation depth is scoped to the merged set**; a clean pull-in is not
re-validated -- but the impact analysis below covers *every* upstream-changed
file, pulled-in ones included: trusting upstream's testing never answers the
local question of who depends on the file.

### 4a. Identify impacted services and skills

No script can enumerate what depends on a changed file -- this is exploration
work, and you must do it for every changed `scripts/**`, `libs/**`, and
`.agents/**` path. Build the impact set like this:

1. **Enumerate the consumer universe** up front, independent of the diff: every
   `supervisord.conf` program (and everything its `command` invokes, directly or
   through a wrapper), every service under `libs/`, every workspace-added skill
   under `.agents/skills/` (e.g. a crystallized `fetch-process-show` pipeline
   whose scripts a daemon or scheduled job runs), and any cron/scheduled
   runners.
2. **Search for dependents of each changed file**: grep the repo for its path,
   its basename, and its importable module name; follow each service's code
   into the shared scripts and libs it calls; check skills' `SKILL.md` and
   scripts for references.
3. **Reason about interface-level coupling that no grep will find.** If the
   diff changes an API surface -- the system_interface HTTP API, a shared data
   file's format, a script's CLI flags -- ask who *calls* that surface: a local
   service built against the system_interface API is impacted even though no
   file of it references the changed one.
4. **Bias toward "impacted" when uncertain**, and record in your report what
   you checked and how, so the lead sees the coverage instead of trusting an
   unstated search.
5. **When you label a lib or skill "workspace-added," verify it -- do not infer
   it from the directory.** "Not under `vendor/`" does **not** mean
   workspace-added: most `libs/` and `.agents/skills/` entries are built-in
   template code. A path is built-in if it exists at the target ref; check
   before labeling: `git ls-tree -r --name-only "$TARGET_REF" -- libs/<name>`
   (empty output = genuinely workspace-added). This matters because only
   genuinely workspace-added code is un-validated-by-upstream -- mislabeling
   built-in code as workspace-added misattributes pre-existing issues (a failing
   test or lint error) as the user's when they are the upstream release's, and
   the lead's approval message repeats the error.

**Provisioning files always count as impacted -- and you best-effort apply them.**
A change to `scripts/setup_system.sh`, `scripts/install_secret_scanners.sh`,
`scripts/_provision_guard.sh`, or `.mngr/**` (the `provisioner` reveal class) has
no *running* consumer to grep for -- nothing imports it -- yet it installs and
configures the global toolchain (the latchkey CLI, uv, claude, modal, the secret
scanners) and the `mngr create` config every live agent, service, and future
sub-agent runs on. So never conclude "nothing to reveal" for one. Work each
provisioning change through, most-live-applicable first, and record the plan in
your report so the lead can carry it out (you stay in your worktree -- you make
the in-repo edits an apply implies, but the lead runs the live restarts):

- **Toolchain-script pins** (`setup_system.sh` / `install_secret_scanners.sh`) --
  a pinned-version bump (e.g. `LATCHKEY_VERSION`) is **live-applicable**: the lead
  re-runs the idempotent provisioner (`bash scripts/setup_system.sh`) to install
  the new version. A hunk only a fresh image build reproduces is **rebuild-only**.
- **`.mngr/**` settings** -- `.mngr/settings.toml` only governs `mngr create`, so
  the merged file governs every *future* create automatically (a new workspace,
  and the sub-agents `launch-task` spawns). But the *current* workspace was built
  and launched under the **old** settings, so a create-time change does not reach
  it on its own. **Examine each changed setting and best-effort make it live:**
    Lean hard toward applying live: most settings have a live counterpart, and
    "it's fiddly to get right" is not a reason to defer -- only a genuine lack of
    any live lever is.

    **Ground every apply in how `vendor/mngr` consumes the setting -- do not guess
    the live mechanism.** For each changed key, grep `vendor/mngr` for its name to
    find exactly where mngr reads and enacts it at create time, then mirror *that*
    mechanism. E.g. a `commands.create` `host_env__extend` change: `grep -rn
    host_env vendor/mngr` shows where mngr turns those entries into the agent
    container's environment (which env file / process env it writes), so you know
    the precise place to set them live and which process must restart to re-read
    them. Likewise `settings_overrides` -> where mngr writes Claude's settings;
    `extra_provision_command` -> how/when mngr runs it; `disable_plugin` -> where
    the plugin list is applied. Applying the setting the way mngr itself does is
    what makes the live edit correct rather than a plausible-looking guess.

    Cases, most-clearly-applyable first:
  - **Env vars and agent behavior** (`host_env` / `pass_env` / `pass_host_env` /
    `env`, `settings_overrides` like `model` / `fastMode`, `disable_plugin`) are
    **live-applicable**, just fiddly: they shape the environment and config that
    each agent/service process reads *at launch*, so mirror the change into the
    live equivalent (an env var into a `profile.d` entry or the relevant
    supervisord program's `environment=`; an agent-behavior override into whatever
    the running agent reads) and have the lead bounce the consumers -- `mngr start
    --restart system-services`, or a relaunch of the affected agent -- so the next
    process start picks it up. Do the mirror edit in your branch so it merges and
    is validated. Get it right rather than punting it to a rebuild.
  - A **toolchain/version pin** under `[agent_types.*]` (Claude version) -> mirror
    into `setup_system.sh` / the `Dockerfile` pin so a provisioner re-run installs
    it, and bounce the services agent. An `extra_provision_command` addition -> the
    lead runs that command live. Keep lockstep pins (`agent_types.claude.version`
    vs the Dockerfile `CLAUDE_CODE_VERSION` and the installed binary) consistent
    across every file that carries them.
  - Only a **container build/launch parameter** an already-running container
    genuinely cannot adopt -- a `[create_templates.*]` / `[providers.*]`
    `build_arg`, a `start_arg` (`--security-opt`, `--tmpfs`, `--workdir`,
    `--cpus`/`--memory`/`--disk`, `--restart`), or a runtime/provider flag (`runsc`
    / `docker_runtime` / `install_gvisor_runtime`) -- is **rebuild-only for the
    current workspace** (it still governs future creates). Flag it to the lead as
    needing a workspace recreate, exactly like an image-level `Dockerfile` hunk; do
    not imply it is already in effect.

**Escape hatch (`stuck`).** If a provisioning change is **not** live-applicable
**and** leaving the running workspace on the old provisioning would **genuinely
break it** (not merely "won't take effect until the next create"), do **not**
report `done` with a rebuild flag -- report `stuck` (Step 6), name the setting and
why it breaks, and refuse the update so the live workspace is left untouched.
Reserve this for real breakage; a change that is simply deferred-until-rebuild is
`done` plus a rebuild flag, not `stuck`.

**A global-dependency bump with a dependent -- safety turns on who depends on it.**
When a merge bumps a *global* dependency (a `setup_system.sh` /
`install_secret_scanners.sh` pin, or a `Dockerfile` toolchain pin), whether it is
safe to apply live depends on **who consumes the new version**. Your worktree
cannot itself validate the pair -- worktree isolation isolates the *repo tree*,
not the host-global toolchain, so your env still has the **old** dep; do **not**
globally install the new one to test, that mutates the shared toolchain the live
workspace and other agents run on. So decide by the **provenance** of the
dependent -- does its code come from the upstream template, or was it built in
this workspace? Decide this by *origin, not directory*: path is not the signal
(a workspace's own `build-web-service` app lands as a new lib under `libs/`, right
alongside the template's built-in `libs/*` services). The check is whether the
dependent's code exists in upstream at the target ref -- e.g. `git cat-file -e
"$TARGET_REF":<path>` for its files, or whether it's part of the merge base's
template rather than added locally.

- **Dependent is built-in code** (present in the upstream template at the target
  ref -- e.g. `apps/system_interface`, a template-shipped `libs/*` service, a
  `.agents/shared/` script): **classify it live-applicable and report that** -- the
  upstream release tested that built-in code against the bumped dependency
  *together*, so it's safe to apply on the same "trust upstream's testing" basis
  the whole pulled-in set rides on. Not rebuild-only. **You do not run the bump
  yourself:** re-running the provisioner is a live, host-global toolchain mutation
  you can't (and mustn't) do from your worktree -- it's the **lead's** action at
  reveal. Your job here is only to judge it safe-to-apply and say so in the report
  (name the provisioner re-run + the built-in service the lead should restart); you
  don't validate the built-in against the new dep either, because you're trusting
  upstream's testing rather than re-doing it.
- **Dependent is user-created** (absent from upstream -- built in this workspace:
  a `build-web-service` app in its own `libs/` lib, a crystallized skill's scripts
  under `.agents/skills/<skill>/`, a local script): **unsafe to hot-apply.**
  Upstream never saw that code, so it never tested it against the new dependency,
  and you can't either (shared toolchain). Classify it **rebuild-only** -- the safe
  way to land it is a workspace recreate, which provisions the new substrate and
  re-runs the user code against it. If leaving it unapplied would break the running
  workspace, that's `stuck`.

For either case:
- **Research the version change online** to ground your assessment: look up the
  dependency's release notes / changelog for the exact old -> new delta (breaking
  changes, removed flags, changed behavior, new minimum runtimes). Don't rely on
  memory -- fetch the actual notes and record what you found and how it bears on
  the dependent (this is what tells you whether a *user* dependent is likely fine
  or genuinely at risk).
- **Report the coupling** explicitly: which dependent, whether it's built-in or
  user-created, what you could/couldn't validate, and your apply / rebuild-only /
  `stuck` call.

An impacted *service* gets validated below (boot + suites) and flagged for
restart in your report. An impacted *skill* (a workspace-added skill relying on
something the update changed) gets validated per its own contract -- run its
tests, or exercise its scripts -- and called out in the report.

### 4b. Validate

- **Environment gate first**, whenever a manifest or lockfile is in the merged
  set (in particular after any lock you regenerated):

  ```bash
  uv lock --check          # lock parses and matches the merged pyproject
  uv sync --all-packages   # env actually builds from it
  ```

  A failure here is a precise blocker -- fix the lock/manifest before running
  anything else, or a corrupt or manifest-stale lock surfaces later as a
  confusing `uv run pytest` explosion. This maps 1:1 to the worst live failure
  mode: `bootstrap` is `uv run`-launched, so an unparseable root lock means no
  service in the workspace can start.
- **Suites/lint/ratchets** for each project in `projects_to_validate`: root `.`
  (`uv run pytest` + `uv run ruff check`) covers `libs/**`, `scripts/**`,
  `.agents/**`; `apps/system_interface` runs its own `uv run pytest` (and `npm run
  lint && npm run test` when the frontend merged); `vendor/mngr` its own `uv run
  pytest`.
- **Isolated-service boots** for each impacted service (per 4a) -- boot against a
  scratch data copy via `.agents/shared/scripts/serve_isolated_instance.py` (see
  `update-service`), never the live store; a service that won't boot on the merged
  code is a blocker. Note this boot runs on the **host's global toolchain**, so it
  does *not* exercise a global-dependency bump -- a service coupled to one is the
  gap covered by the coupled-change note in 4a, not something an isolated boot
  can close.
- **Playwright** for a web surface -- system interface *or* a user service -- only
  when the merge needed nontrivial merge work there (not a clean pull). For the
  system interface, build it in your worktree (`cd apps/system_interface && uv
  sync && npm run build`) so your work_dir is a built instance the lead can
  preview, then drive it per
  `.agents/shared/worker/references/web-frontend-testing.md`.

### 4c. Review gates

Run the repo's review gates on the merged result, like every other harden pass:
follow the "Review gates" section of
`.agents/shared/worker/references/harden-artifact.md` (unattended `/autofix`,
then judge each fix commit yourself -- keep by default, revert only what undoes
intended behavior -- plus the architecture gates). Record kept/reverted fixes
and gate verdicts for your report.

## 5. Gather the "what's new" inputs

```bash
python3 runtime/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    changelog-entries --base "$BASE" --target "$TARGET_REF"
```

## 6. Report back

Per `.agents/shared/references/worker-reporting.md` (`<TASK_FILE_GLOB>` ->
`runtime/update-self/task.md`; `<RUNTIME_REPORTS_DIR>` ->
`runtime/update-self/reports`). Valid `name:` values:

- `question` (`type: gate`) -- a genuine, unresolvable conflict; body: the file,
  what each side did, the options. Push and stop; resume on the lead's reply.
- `done` (`type: status`) -- merged, triaged, validated on `mngr/update-self`. Body
  gives the lead everything for the approval gate and reveal:
  - **What's new** -- a digest of the changelog entries.
  - **Conflicts** -- each one and how you resolved it.
  - **Merged vs pulled-in** -- which reveal classes reconciled vs came in clean.
  - **Merge work per web surface** -- for the system interface and each user web
    service: "none" (upstream strictly newer, clean pull) or "nontrivial" with a
    sentence on what had to be reconciled. The lead previews a surface if and
    only if you judged its merge work nontrivial, so judge this explicitly.
  - **Impact analysis** -- the impacted services and skills from 4a, what you
    checked and how, and which services the lead must restart.
  - **Dockerfile split** (if it merged) -- each hunk as live-applicable (e.g. a
    `CLAUDE_CODE_VERSION` bump) or image-level (needs a manual rebuild).
  - **Provisioning changes** (if any `provisioner`-class file changed) -- per the
    impact analysis above, each change with its apply plan: **live-applicable**
    (the in-branch edits you made to mirror it + the exact restart the lead runs,
    e.g. a `LATCHKEY_VERSION` bump -> re-run `setup_system.sh`; an env var or
    `[agent_types]` change -> mirrored into the live env/pin + `mngr start
    --restart system-services`) or **rebuild-only for the current workspace** (only
    a `build_arg` / `start_arg` / runtime-flag change a running container can't
    adopt). A genuinely-breaking, unapplyable change is a `stuck` report, not a
    `done`.
  - **Global-dependency bump with a dependent** (if the merge bumps a global dep
    that something depends on) -- the version delta and what your online research
    turned up, **which dependent(s)** and whether each is **built-in** (its code is
    in upstream at the target ref, so upstream-tested -> apply live) or
    **user-created** (absent from upstream, built in this workspace; couldn't
    validate -> rebuild-only, or `stuck` if it would break the running workspace).
    Judge by origin, not directory. Call out any gap honestly; the lead applies the
    built-in case and does not hot-apply the user case.
  - **Validation** -- suites/boots/Playwright and review gates run, all passing;
    autofix fixes kept vs reverted; and any validation **gap** (a coupled bump you
    couldn't fully exercise) called out honestly rather than implied as covered.
- `stuck` (`type: status`) -- you couldn't reach a clean, validated merge, or you
  hit the provisioning escape hatch above (a change you can neither apply live nor
  safely defer to a rebuild without breaking the running workspace); one sentence
  on what blocked you and where the work stands. Never report `done` on a merge
  whose suites or boots fail.
