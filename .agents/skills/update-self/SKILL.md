---
name: update-self
description: Pull updates from the upstream template repo safely. Dispatches a background worker that merges the latest released template version on its own branch, validates the impacted services, and reports what changed; the lead confirms with you and then applies the update live (restarting the services that need it). Use when you want to incorporate upstream skills, script fixes, or config improvements. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead.
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

REF=$(python3 .agents/skills/update-self/scripts/update_self.py resolve-target \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["ref"])')
# honoring a user override, append e.g. `--override main` or `--override minds-v0.3.6`
# to the resolve-target call above.
echo "$REF"
```

`resolve-target` prints `{"ref": ..., "kind": "tag|branch|ref"}`; `main` resolves
to `upstream/main` (not the stale local branch). Keep `$REF` in your shell for the
dispatch below, and tell the user which version you're updating to.

## 3. Dispatch the worker

Open a tracking ticket, write the task file, launch via the `launch-task`
machinery, and background-poll.

```bash
mkdir -p runtime/update-self
TICKET_ID=$(tk create "update-self" -t task \
    --acceptance "worker launched; conflicts triaged; validated; branch merged; revealed")
tk start "$TICKET_ID"
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

## 5. Terminal status

- **`stuck`** or a dead-worker timeout -> surface via
  `.agents/skills/launch-task/references/worker-failure.md`. Nothing is merged or
  revealed; the live workspace is untouched.
- **`done`** -> the approval gate below.

### 5a. Approval gate

The `done` report is a "what's new" summary: the new changelog entries in range,
the conflicts and how the worker resolved them, the services it validated, and
anything flagged for a manual rebuild. Present it via `send-user-message` and
**wait for explicit approval** -- mandatory even on a clean pull.

If the merge reconciled the system interface for real (not a clean pull-in), also
give the user a live preview first, exactly as `update-system-interface` Step 3
does (keep the worker alive until they verdict):

```bash
WORK_DIR=$(mngr ls --include 'name == "update-self"' --format json \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["agents"][0]["work_dir"])')
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview \
    --slug update-self --work-dir "$WORK_DIR"
python3 scripts/layout.py open si-preview
```

### 5b. Land the merge

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

  Exit codes per `update-system-interface` Step 5 (`0` revealed; `2`
  auto-rolled-back; `3` emergency; `1` precondition).

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

- **`shared_runtime` (`scripts/**`, other `libs/**`, `.agents/**`)** -- applies to
  future agents automatically unless a live service depends on the file. The
  report's downstream-consumer trace names any live consumer; restart that
  service (usually `mngr start --restart system-services`). Only "nothing to
  reveal" when the trace found none.

## 6. Teardown

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name update-self
tk close "$TICKET_ID" "Updated to <ref> -- worker branch merged and revealed."
```

## To push local improvements back upstream

Use the `submit-upstream-changes` skill -- the complementary direction. This skill
only pulls.
