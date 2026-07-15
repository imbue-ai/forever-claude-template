# Update-self worker guidance

You are the background worker for a safe update-self pass, in your own worktree
branched off the lead's `HEAD`. Merge the target upstream ref, triage the
conflicts, validate what reconciled, and report a "what's new" summary. **You
never restart a live service or reveal anything** -- you validate in isolation
and report; the lead applies the update.

The deterministic pieces live in
`.agents/skills/update-self/scripts/update_self.py` -- call it, don't reimplement.

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

Triage each conflict:

- **Agent-specific files -> keep local** (`PURPOSE.md`, agent-specific `CLAUDE.md`
  sections, `runtime/`): `git checkout --ours -- <path> && git add <path>`.
- **Files untouched locally -> take upstream**: `git checkout --theirs -- <path>
  && git add <path>`.
- **Genuine ambiguity** (both sides changed the same region incompatibly and the
  answer depends on user intent) -> write a `name: question` gate (Step 6)
  describing the file, what each side did, and the options; push and stop. The
  lead relays the user's decision; apply it and continue.

Do not gate on anything the two rules resolve. No conflicts at all means a clean
pull -- go straight to committing.

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
re-validated.

- **Suites/lint/ratchets** for each project in `projects_to_validate`: root `.`
  (`uv run pytest` + `uv run ruff check`) covers `libs/**`, `scripts/**`,
  `.agents/**`; `apps/system_interface` runs its own `uv run pytest` (and `npm run
  lint && npm run test` when the frontend merged); `vendor/mngr` its own `uv run
  pytest`.
- **Isolated-service boots** for each impacted service -- boot against a scratch
  data copy via `.agents/shared/scripts/serve_isolated_instance.py` (see
  `update-service`), never the live store; a service that won't boot on the merged
  code is a blocker.
- **Playwright** for a web surface -- system interface *or* a user service -- only
  when the merge reconciled real divergence (not a clean pull). For the system
  interface, build it in your worktree (`cd apps/system_interface && uv sync &&
  npm run build`) so your work_dir is a built instance the lead can preview, then
  drive it per `.agents/shared/worker/references/web-frontend-testing.md`.
- **Downstream-consumer trace** for each changed `scripts/*` or `.agents/**` file:

  ```bash
  python3 .agents/skills/update-self/scripts/update_self.py trace-consumers --path <changed-path>
  ```

  A non-empty `programs` list means a live service shells out to it -- validate it
  like a service change and note that the lead must restart that service. Bias
  toward flagging a restart for transitive/scheduled invocations the direct trace
  can't see.

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
  - **Services to restart** -- impacted services, plus any live consumer of a
    changed shared file.
  - **Dockerfile split** (if it merged) -- each hunk as live-applicable (e.g. a
    `CLAUDE_CODE_VERSION` bump) or image-level (needs a manual rebuild).
  - **Validation** -- suites/boots/Playwright run, all passing.
- `stuck` (`type: status`) -- you couldn't reach a clean, validated merge; one
  sentence on what blocked you and where the work stands. Never report `done` on
  a merge whose suites or boots fail.
