# Update-self worker guidance

You are the background worker for a safe update-self pass, in your own worktree
branched off the lead's `HEAD`. Merge the target upstream ref, triage the
conflicts, validate what reconciled, and report a "what's new" summary. **You
never restart a live service or reveal anything** -- you validate in isolation
and report; the lead applies the update.

The deterministic pieces (target resolution, merged-vs-pulled classification,
changelog gathering) live in
`.agents/skills/update-self/scripts/update_self.py` -- call it, don't
reimplement. Impact analysis (who depends on a changed file) is deliberately
*not* scripted; Step 4a is your recipe for it.

## 1. Resolve inputs

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py 'runtime/update-self/task.md')"
```

Sets `LEAD_AGENT`, `FINISH_REPORT_PATH`, `TARGET_REF`. If the worktree has no
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
python3 .agents/skills/update-self/scripts/update_self.py \
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
python3 .agents/skills/update-self/scripts/update_self.py \
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
  code is a blocker.
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
python3 .agents/skills/update-self/scripts/update_self.py \
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
  - **Validation** -- suites/boots/Playwright and review gates run, all passing;
    autofix fixes kept vs reverted.
- `stuck` (`type: status`) -- you couldn't reach a clean, validated merge; one
  sentence on what blocked you and where the work stands. Never report `done` on
  a merge whose suites or boots fail.
