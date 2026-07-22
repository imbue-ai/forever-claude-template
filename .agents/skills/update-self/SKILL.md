---
name: update-self
description: Safely pull updates from the upstream template repo (default target is the latest stable release). Use when you want to incorporate upstream skills, script fixes, or config improvements. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead.
---

# Pulling updates from the upstream template, safely

This repo was created from a template repo and stays connected to it via a git
remote (`parent.toml` has the URL and branch). Upstream carries the shared
infrastructure: skills, scripts, `CLAUDE.md` scaffolding, `Dockerfile`,
`supervisord.conf`, the system interface, the vendored `mngr`.

Merging upstream can break the live workspace -- a settings-schema change the
running `system_interface` can't parse, a bumped `vendor/mngr`, a new service.
So, like `update-system-interface`, this flow never mutates the live tree from an
unverified state: an isolated **worker** does the merge and validation on its own
branch, and only a known-good, user-approved result is landed and applied.

You are the **lead**: resolve the target, dispatch the worker, proxy its one
gate, present the approval gate, and -- on approval -- land the merge and reveal
each change by its class. The worker owns the merge, the conflict triage, and the
validation; you own going live.

The default target is the **latest stable `minds-v*` tag** (released,
already-tested), not `origin/main`. The user may override to a specific tag or to
`main`.

## 1. Preconditions

**Back up first.** Before dispatching anything, capture a restore point of the
whole workspace so the pass is recoverable -- the reveal re-runs provisioners and
restarts services, and a backup is the recovery path if one of those goes wrong:

```bash
uv run host-backup-now
```

It waits for any in-flight backup, forces a fresh tick, and prints the
`restic_backup_succeeded` / `restic_backup_failed` event -- confirm success before
continuing. If it reports backups aren't configured
(`tick_skipped_due_to_missing_secrets` -- no `runtime/secrets/restic.env`), there
is **no** restore point: tell the user, and get their explicit go-ahead before
proceeding without one.

**Single-flight.** One pass at a time (its worker name, branch, and runtime dir
are fixed). Check for a live one:

```bash
tk ready > /tmp/update-self-inflight.txt
grep "update-self" /tmp/update-self-inflight.txt
```

If a live `update-self` ticket exists, stop and tell the user; if it looks
abandoned, take it over per `.agents/shared/references/harden-contention.md`.

**Clean tree.** The worker branches off your `HEAD` and the rollback captures it.
If `git status --porcelain` is non-empty, surface it and stop.

## 2. Resolve the target

Ensure the remote exists, fetch with tags, and resolve the ref:

```bash
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
git fetch upstream --tags

REF=$(python3 .agents/skills/update-self/scripts/update_self.py resolve-target --local-tags \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["ref"])')
# `--local-tags` reads the tags the fetch above just landed (no second network
# round-trip). Honoring a user override, append e.g. `--override main` or
# `--override minds-v0.3.6` to the resolve-target call above.
echo "$REF"
```

`resolve-target` prints `{"ref": ..., "kind": "tag|branch|ref"}`; `main` resolves
to `upstream/main` (not the stale local branch). Keep `$REF` in your shell for the
dispatch below, and tell the user which version you're updating to.

To preview what the release actually changes, always diff from the **merge
base**, never from `HEAD` -- a `git diff HEAD "$REF"` also shows every *local*
change as if upstream were reverting it, which reads as phantom upstream churn:

```bash
git diff --name-status "$(git merge-base HEAD "$REF")" "$REF"
```

## 3. Dispatch the worker

Open a tracking ticket, write the task file, launch via the `launch-task`
machinery, and background-poll.

```bash
mkdir -p runtime/update-self
tk create "update-self" -t task \
    --acceptance "worker launched; conflicts triaged; validated; branch merged; revealed"
```

Note the ticket id it prints, then start it. The tk hook requires `tk start` /
`tk close` to be the *only* command in their tool call -- never chain them after
another command or capture their output:

```bash
tk start <ticket-id>
```

Write the task file. Use the two-heredoc form the other worker skills use: an
**unquoted** frontmatter block so `$MNGR_AGENT_NAME` and `$REF` expand, then a
**quoted** body so its backticks stay literal:

```bash
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/update-self/reports/report.md
target_ref: $REF
---
FRONTMATTER_EOF
cat << 'BODY_EOF'

# Task: safe update-self

## What to do
Follow `.agents/skills/update-self/references/update-self-worker.md` end to end:
trial-merge conflict triage, complete the merge (preserving the `update-self:`
merge-commit subject), validate the merged set, generate the "what's new" report,
and report `done`. Your target is the `target_ref` in this file's frontmatter
(already fetched into `upstream`).

## Reporting back
Per `.agents/shared/references/worker-reporting.md`. Valid `name:` values:
`question` (mid-flight gate for a genuine, unresolvable conflict), `done` /
`stuck` (terminal). Substitutions: `<TASK_FILE_GLOB>` -> `runtime/update-self/task.md`;
`<RUNTIME_REPORTS_DIR>` -> `runtime/update-self/reports`.
BODY_EOF
} > runtime/update-self/task.md
```

Launch with the plain `worker` template (this flow uses its own worker guidance,
not the generic `harden-worker`), then background-poll (`run_in_background:
true`), re-arming per `lead-proxy.md`:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-self --template worker \
    --runtime-dir runtime/update-self/ --task-file runtime/update-self/task.md

uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --name update-self --task-file runtime/update-self/task.md --timeout 90m
```

## 4. Proxy the `question` gate

Per `.agents/shared/references/lead-proxy.md` (worker `update-self`, branch
`mngr/update-self`, reports dir `runtime/update-self/reports/`). The worker
surfaces only genuine, unresolvable conflicts -- a real decision about how to
reconcile a file both sides rewrote incompatibly. **Escalate it to the user**,
relay their resolution via `mngr message`, consume the report, and re-arm.

**Compose the question per the §5a rules -- plain-language and pointed at a
resolution, not the worker's raw conflict dump.** Lead with where things stand
("The update is almost ready -- one file needs a decision from you before I can
finish"), explain the choice in plain terms (what the new version does vs. what
your workspace currently does, and what's at stake each way), and **propose a way
forward**: a recommended option when you have one, the concrete trade-offs when
you genuinely don't. Close by inviting the user to resolve it *with* you rather
than only to rule on it -- "tell me which you'd prefer, or talk it through with me
and we'll land on the best option together." Reassure that nothing has been
applied and the workspace is untouched.

## 5. Terminal status

- **`stuck`** or a dead-worker timeout -> surface via
  `.agents/skills/launch-task/references/worker-failure.md`. Nothing is merged or
  revealed; the live workspace is untouched. **Don't relay the raw failure, but
  don't strip the specifics either** -- this is the one message type where
  technical detail is *preserved, not dropped*, because the user often forwards a
  failure into a bug report and it must stand on its own to whoever reads it next.
  Compose it in two parts: a **plain-language lead for the user** (what happened
  and what it means -- "I couldn't complete this update cleanly; your workspace is
  untouched and nothing was applied" -- plus a next step or an offer to work
  through it together), followed by a **clearly-marked technical detail block for
  whoever they escalate to**: the target ref, the step or phase that failed, the
  specific file or component, and the **actual error text or log excerpt
  verbatim** (not paraphrased), with a pointer to the full report and logs under
  `runtime/update-self/reports/`. Never leave the user at a dead end, and never
  hand them a failure so vague it's useless in a bug report.
- **`done`** -> the approval gate below.

### 5a. Approval gate

The `done` report is *your* raw material, not the user's message. It is a
comprehensive, technical digest for the lead -- changelog entries in range, the
conflicts and how the worker resolved them, reveal-class breakdown, impact
analysis, lockfile handling, and validation. **Do not forward it verbatim.**
Keep it available (it is persisted under `runtime/update-self/reports/` -- offer
to show it if the user wants the specifics), and **compose a plain-language
approval message** from it. Then **wait for explicit approval** -- mandatory even
on a clean pull.

**These composition rules govern every user-facing message this flow produces --
the approval message here, the `question` gate (Step 4), and a `stuck` result
(Step 5) alike.** Whenever the update can't simply proceed, the message names the
blocker in plain terms and **proposes a way forward, or invites the user to
resolve it with you** -- it never dead-ends. The one thing that varies is how
much mechanism to keep: the approval and `question` messages drop technical
detail the user can't act on, but a `stuck` message deliberately preserves it
(Step 5) so it survives being pasted into a bug report.

Write the message a non-technical reader skims top-to-bottom, in this fixed
order:

1. **Verdict headline** (one line, first thing they see): "ready to apply,"
   "ready to apply, with one caveat," or "needs your input on X."
2. **What's new** -- always first after the headline. Keep this *detailed*: some
   readers want the specifics, others happily skim it as "great, they're on it."
   Do not thin it out -- carry the worker's digest, just in prose a lay reader
   parses (describe what each change does, not the file names).
3. **Conflicts** -- "none," or what needed reconciling.
4. **Validation** -- did the suite pass; is any failure pre-existing/unrelated.
5. **Caveats** -- only if any; what to expect after applying.
6. **Pre-existing issues** -- only if any, and only after verifying attribution
   (see the worker guidance's §4a): state plainly whether each lives in
   **built-in** code (present at the target ref -> report upstream) or the
   **user's own** code. Never call built-in code "workspace-added."
7. **The ask** -- see the language rule below.

**Detail in the informational sections (2-4); plain language at the decision
points.** Spend deliberate plain-language care only where the message asks the
user to **decide or act** -- the verdict headline, any caveat that needs their
action, and the closing ask. Those carry no jargon: never "merge," "land," or
"fast-forward" there. Frame the ask around *applying the update to their
workspace* (many users just want their workspace improved and don't think in
terms of merges), e.g. "Everything's ready -- want me to apply the update to
your workspace now?"

**Drop dependency/lockfile mechanics** from the user message unless there is an
action the *user* must take. **Command rule:** never print a command *you* will
run -- describe it in plain language ("I'll refresh it automatically if it comes
up"). Show a literal command only when the *user* must run it themselves.

**Preview rule for the system interface:** if upstream was strictly newer there
(no merge work needed), no preview is needed; if the worker's report says
nontrivial merge work was needed, give the user a live preview first, exactly as
`update-system-interface` Step 3 does (keep the worker alive until they
verdict). The report's per-surface merge-work judgment is what you go by.

```bash
WORK_DIR=$(mngr ls --include 'name == "update-self"' --format json \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["agents"][0]["work_dir"])')
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview \
    --slug update-self --work-dir "$WORK_DIR"
python3 scripts/layout.py open si-preview
```

**Other web services are optional previews.** When the report says another user
web service took meaningful merge work, use your judgment: serve it from the
worker's worktree via `.agents/shared/scripts/serve_isolated_instance.py` as its
own preview tab -- or, when the system interface is also being previewed, link
it from inside that preview. Skip previews for services that came in clean.

### 5b. Land the merge

**When the update touches `apps/system_interface/` at all** (merged *or* pulled
in -- anything that makes 5c run the safe-reveal), first take the
`editing service system_interface` lease and hold it through the end of 5c, for
the same reason the `update-system-interface` flow holds it across merge + reveal:
the reveal's auto-rollback restores a captured revision, so a foreign merge
landing between here and the reveal could be swept away by it. Check `tk ready` for another agent's lease
and surface instead of proceeding if one is held; then `tk create "editing
service system_interface" -t chore` and `tk start` it (each as its own
command). Release it (tk close) after 5c.

Capture the rollback revision, then fast-forward the worker branch. It branched
off this exact `HEAD`, so the merge fast-forwards and **preserves the worker's
`update-self:` merge commit verbatim** (the marker `assist` relies on):

```bash
ROLLBACK_TO=$(git rev-parse HEAD)
git fetch . mngr/update-self:mngr/update-self   # materialize the worker branch locally
git merge --ff-only mngr/update-self
```

If the fast-forward is refused, `HEAD` moved under the pass: treat it as stale
per `.agents/shared/references/harden-contention.md` and re-dispatch off the
current `HEAD` -- do not hand-resolve.

### 5c. Reveal by change class

The report says which classes merged. Apply each; a clean pull-in is still
*applied* (its dependent service restarted), only its validation was skipped.

- **`system_interface`** -- reveal via the safe-reveal script (rebuilds `static/`,
  refreshes deps on a manifest change, pre-flights, health-checks,
  auto-rolls-back), then tear down any preview:

  ```bash
  python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py reveal \
      --rollback-to "$ROLLBACK_TO"
  python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py unpreview --slug update-self
  python3 scripts/layout.py close si-preview
  ```

  Exit codes per `update-system-interface`'s reveal step (`0` revealed; `2`
  auto-rolled-back; `3` emergency; `1` precondition). **On exit 2 the rollback
  reverts `$ROLLBACK_TO..HEAD` -- the entire landed merge, every class, not
  just the system interface.** Stop here: apply no other class (the tree no
  longer contains the update), surface the failure, and re-dispatch once the
  cause is fixed. Exit 3 means the restore itself failed -- surface immediately.

- **`service` / `supervisord.conf` / `bootstrap`** -- restart the services agent
  so `bootstrap` re-runs and `supervisord` reloads every program, then refresh
  any affected tab (`python3 scripts/layout.py refresh <name>`):

  ```bash
  mngr start --restart system-services
  ```

- **`editable_tool` (`vendor/mngr/**`)** -- `.py` is picked up live; a manifest
  change needs an env refresh (`uv sync --all-packages`, or `uv tool install -e
  vendor/mngr --reinstall` for a tool entry point). Any other `is_manifest` change
  the report flags (a root-workspace `pyproject.toml` / `uv.lock`) likewise needs
  `uv sync --all-packages` so the new dependencies resolve.

- **`Dockerfile`** -- apply the live-applicable hunks the report calls out
  (canonically a `CLAUDE_CODE_VERSION` bump -> `CLAUDE_CODE_VERSION=<v> bash
  scripts/setup_system.sh`, keeping `agent_types.claude.version` in
  `.mngr/settings.toml` in sync). Tell the user any image-level hunk (base
  `FROM`, `apt-get` packages, build-time layout) needs a manual workspace rebuild.

- **`provisioner` (`scripts/setup_system.sh`,
  `scripts/install_secret_scanners.sh`, `scripts/_provision_guard.sh`,
  `.mngr/**`)** -- shapes how the workspace image and agents are *provisioned*,
  not live runtime code, so it doesn't reveal by merely restarting a dependent
  service the way `shared_runtime` does. Work the report's apply plan by sub-case:

  - A **pinned-toolchain bump** in `setup_system.sh` /
    `install_secret_scanners.sh` (canonically `LATCHKEY_VERSION`, but also `UV_`,
    `MODAL_`, `TTYD_`, `CLOUDFLARED_`, scanner pins) does **not** reach the live
    workspace on its own -- the globally-installed CLI stays at the old version
    until a rebuild. Apply it live by re-running the provisioner:

    ```bash
    bash scripts/setup_system.sh
    ```

    This now actually runs (rather than skipping): the merge changed the repo
    tree, so the content-addressed provision guard's marker no longer matches,
    and the script re-installs the pinned tools idempotently. The report names
    which pins moved.

    **Exception -- a bump the report flags as coupled to a *user-created*
    dependent.** The report says who depends on the bumped dep and classifies it
    by origin (not directory). If the dependent is **built-in** (its code is in the
    upstream template -- the same release tested it against the new dep),
    hot-running the provisioner is safe; apply it live as above. If the dependent
    is **user-created** (built in this workspace, absent from upstream -- e.g. a
    `build-web-service` app, which also lives under `libs/`), do **not** hot-run
    the provisioner: upstream never tested that code against the new dep and the
    worker couldn't validate it either, so treat it as **rebuild-only** -- surface
    it to the user for a workspace recreate (which provisions the new substrate and
    re-runs the user code against it), exactly as an image-level hunk below.
  - A hunk that only affects a **fresh image build** -- something the idempotent
    re-run does not reproduce -- needs a **manual workspace rebuild**; tell the
    user, exactly as for an image-level `Dockerfile` hunk.
  - **`.mngr/**` create config** governs `mngr create`, so the merged file
    governs every *future* create automatically (a fresh workspace, and the
    sub-agents `launch-task` spawns) -- but the *current* workspace was built and
    launched under the **old** settings, so a create-time change does not reach it
    on its own. The worker's report carries a **per-change apply plan** (it
    best-effort mirrors each change into a live counterpart within the merge);
    carry it out:

    - **Live-applicable** (most changes, including env vars and agent behavior) --
      the worker already made the in-repo edits mirroring the change into its live
      counterpart (an env var into a `profile.d` entry / a supervisord program's
      `environment=`; a `settings_overrides` / `disable_plugin` change into what
      the running agent reads; a Claude/toolchain version pin into `setup_system.sh`
      / the Dockerfile). You run the restart the report names to make them take
      effect: re-run the provisioner for a mirrored toolchain pin, and/or `mngr
      start --restart system-services` (or a relaunch of the affected agent) so the
      next process start reads it. Keep lockstep pins (`agent_types.claude.version`
      vs the Dockerfile `CLAUDE_CODE_VERSION` and the installed binary) consistent.
    - **Rebuild-only for the current workspace** (the narrow remainder) -- only a
      container build/launch parameter an already-running container can't adopt: a
      `[create_templates.*]` / `[providers.*]` `build_arg`, `start_arg`
      (`--security-opt`, `--tmpfs`, `--cpus`, …), or runtime flag (`runsc` /
      `docker_runtime`). Flag it to the user as needing a workspace recreate,
      exactly as an image-level `Dockerfile` hunk; don't imply it is already live.

    (A change the worker judged neither live-applicable nor safe to defer to a
    rebuild comes back as `stuck`, handled in Step 5's terminal status -- nothing
    is landed.)

- **`shared_runtime` (`scripts/**` other than the provisioning scripts above,
  `libs/**`, `.agents/**`)** -- applies to
  future agents automatically unless a live service depends on the file. The
  report's impact analysis names any live consumer; restart that service
  (usually `mngr start --restart system-services`). Only "nothing to reveal"
  when the analysis found none.

## 6. Teardown

If you previewed a non-system_interface service in 5a, tear that preview down
too: stop its isolated instance and close its tab (`python3 scripts/layout.py
close <name>`). Then:

```bash
mkdir -p runtime/update-self/reports/consumed
mv runtime/update-self/reports/report.md \
    runtime/update-self/reports/consumed/$(date +%s)-done.md
uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name update-self
```

Consuming the terminal report is not optional bookkeeping: `create_worker.py
launch` refuses to start a worker while a leftover report sits at the report
path (a stale one would satisfy the next pass's `await` instantly), so skipping
this breaks the next update-self pass until someone cleans it up.

Close the tracking ticket last (its own tool call, nothing chained):

```bash
tk close <ticket-id> "Updated to <ref> -- worker branch merged and revealed."
```

## To push local improvements back upstream

Use the `submit-upstream-changes` skill -- the complementary direction. This skill
only pulls.
