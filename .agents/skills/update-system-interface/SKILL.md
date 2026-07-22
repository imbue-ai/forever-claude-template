---
name: update-system-interface
description: Canonical flow for changing the system interface (the web workspace UI at apps/system_interface) -- its frontend (dockview shell, chat rendering, progress view) or backend (Flask server, agent discovery, layout ops). Use whenever the user wants to edit, fix, restyle, or add to the workspace UI / chat interface / dockview.
---

# Updating the system interface

`apps/system_interface` is the live web UI the user is looking at right now
(the dockview shell, the chat panels, the progress view). It is, in effect,
**the service that *is* the workspace UI** -- so this skill is the
system-interface *specialization* of `update-service`'s "live loop first, ratify
at turn-end" shape. Everything genuinely shared -- the editing lease, the
demonstrative-artifact (mock) taxonomy, the turn-end harden handoff -- lives in
[`update-service`](../update-service/SKILL.md) and the references it points at.
This skill carries only what the system interface does *differently*.

Everything different traces to one fact: **a broken build here is served
straight to the user as their entire workspace.** That forces three adjustments
to the ordinary live loop:

1. **Code isolation.** You edit an *isolated git worktree*, never the served
   tree -- a half-broken build can never reach the served UI.
2. **The isolated preview instance *is* the user's view.** For an ordinary
   service the user watches the live tab and a preview is an escalation; here the
   live tab is off-limits, so a labeled preview tab is the normal, always-on way
   the user sees the change as you iterate.
3. **Safe-reveal go-live.** Merging is not enough: going live runs a
   health-checked, auto-rollback reveal script so a bad change can never take the
   UI down.

The live loop itself is **lead-driven and cheap**: edit the worktree, build,
refresh the preview in place, iterate with the user. The expensive test + review
gate is deferred out of the loop and runs once, in a background worker, only
after the user approves the shape. This is the whole point -- iterating on the
workspace UI costs a build (seconds), not a harden pass (minutes).

## The hard rule

**Never edit the system-interface tree that is being served to the user.** Do
not run `Edit`/`Write` on files under `apps/system_interface/` in this (the
served) checkout, and do not rebuild or restart the live UI from uncommitted
edits here. Every change is made in a separate, isolated worktree, built and
previewed there, and revealed to the live tree only through the safe-reveal
script once the user has approved and a background worker has hardened it.

## Flow overview

1. **On entry:** take the editing lease and kick off worktree provisioning **in
   the background** while you read the code and pin down the change with the user.
2. **Live loop:** edit the worktree -> build -> refresh the preview in place ->
   surface to the user -> iterate. Commit before each surface, so branch `HEAD`
   always equals the last thing the user saw.
3. **On approval:** hand the approved shape to a background harden worker *on the
   same branch* (two handoff shapes), with an optional final preview of any real
   work the user hasn't seen.
4. **Go live:** freshness-check, capture the rollback point, merge, run the
   safe-reveal script, then tear everything down and release the lease.

The lease is held across the **whole** pass (entry through reveal or
abandonment) -- a deliberate divergence from `update-service`'s per-turn release,
because there is one served UI and one preview tab, so only one system-interface
edit may be in flight at a time.

## 1. On entry: take the lease, provision in the background, clarify the shape

**Take the editing lease first.** It is the *same* advisory lease
`update-service` uses ("One editor at a time"), so a system-interface pass and an
ordinary edit of some other service never collide on it only if they use
distinct names -- here the name is fixed as `editing service system_interface`.
Pre-flight exactly as `update-service` describes:

```bash
tk ready > /tmp/service-leases.txt
grep "editing service system_interface" /tmp/service-leases.txt
```

If a lease exists and `tk show <id>` says it is not yours, tell the user another
chat is mid-change on the workspace UI and let them decide -- do not silently
proceed. If the holder looks abandoned (its agent is no longer running, or it is
hours old with no notes), say so and offer to break it; the lease is advisory and
broken only by the user's call, never silently. **Breaking a stale
system-interface lease also means tearing down its orphaned pass** -- its preview
service and tab, its worktree, and its worker if one exists (see the teardown in
Step 4; run `unpreview` + `layout.py close si-preview`, `git worktree remove`,
and `create_worker.py destroy` for whatever the abandoned pass left behind).
Otherwise, take your own:

```bash
LEASE_ID=$(tk create "editing service system_interface" -t chore \
    -d "Held by $MNGR_AGENT_NAME across the whole live-edit + harden + reveal pass.")
```

then `tk start "$LEASE_ID"` (as its own command). Unlike an ordinary service
edit, this lease deliberately spans the entire pass -- including the waits for
the user's feedback -- and is released only at final teardown (Step 4) or on
explicit abandonment.

**Pick a slug** `$SLUG` for the change. The branch is `mngr/update-$SLUG`; the
lead's editing worktree lives at `runtime/si-live/update-$SLUG/` (gitignored,
and *separate* from the worker's runtime dir so it is never rsynced into the
worker); the worker's runtime dir is `runtime/harden/update-$SLUG/`.

**Kick off provisioning in the background, then start exploring.** The one real
up-front cost is standing up a built worktree; hide it behind the exploration you
were going to do anyway. Launch this as a background task and immediately start
reading the relevant code and clarifying the change's shape with the user:

```bash
git worktree add -b "mngr/update-$SLUG" "runtime/si-live/update-$SLUG" HEAD
cd "runtime/si-live/update-$SLUG" && uv sync --all-packages \
  && (cd apps/system_interface/frontend && npm ci && npm run build)
```

By the time you have an edit to show, the worktree is warm. **How rough the
first previewed pass should be scales with shape-uncertainty, not with "does it
change what the user sees":** an obvious contained change (font, color,
reposition, copy) you implement directly; a redesign / new view / non-obvious
layout starts as a deliberately rough pass for fast signal. Which
demonstrative-artifact *type* to use is the shared taxonomy in
[`interactive-delivery.md`](../../shared/references/interactive-delivery.md)
(phase 5): the embedded workspace UI **defaults to Type 1 (a janky real edit in
the worktree, shown through the real preview)**; reserve Type 2 (a detached
throwaway prototype) for a genuinely standalone new surface where real wiring is
costly and a fake conveys the idea.

## 2. The live loop: edit the worktree, refresh the preview in place

Work entirely inside `runtime/si-live/update-$SLUG/`. If the change renders
markup a person looks at, invoke `frontend-design` before writing it; if it
calls Claude, follow `use-ai-integration` -- same as when the UI was built. The
build/test mechanics for the system interface (in-process backend tests, the
`test_e2e.py` Playwright harness, `npm run build`/`lint`/`test`) are the worker's
job at harden time and are documented in
[`artifact-system-interface.md`](../../shared/worker/references/artifact-system-interface.md);
in the live loop you only need a clean build, not the full gate.

**First round -- boot the preview.** After the first build, boot the worktree as
a labeled preview tab and open it:

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview \
    --slug "update-$SLUG" --work-dir "runtime/si-live/update-$SLUG"
python3 scripts/layout.py open si-preview
```

`preview` boots `uv run system-interface` from the worktree's already-built app
dir on a free port, with layout persistence neutered (it drops `MNGR_AGENT_ID`
so it cannot clobber the live `layout.json`) but agent discovery kept -- so the
user's real conversations render -- and wraps it in a labeled "preview" frame the
user opens as the `si-preview` tab. It never touches the served tree. (It refuses
to boot if another pass's preview is already up rather than hijacking the tab;
surface that and coordinate.)

**Each subsequent round -- refresh in place; the tab never goes blank.** The tab
points at the wrapper page, which never moves. After editing:

- **Frontend-only round:** rebuild, then reload the iframe. No process bounce
  (the inner app serves the rebuilt `static/` bundle straight from disk):

  ```bash
  (cd runtime/si-live/update-$SLUG/apps/system_interface/frontend && npm run build)
  python3 scripts/layout.py refresh si-preview
  ```

- **Backend round (Python / server logic):** additionally bounce the inner app
  process on its existing port, then reload the iframe:

  ```bash
  # (rebuild first if the frontend also changed)
  python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview-refresh \
      --slug "update-$SLUG"
  python3 scripts/layout.py refresh si-preview
  ```

  `preview-refresh` restarts only the inner app on the same port and re-runs the
  health check; the wrapper frame and the user's tab are untouched. If it exits
  non-zero the new build did not boot -- the tab will show an error until you fix
  it and refresh again; the *live* UI is unaffected either way.

**Commit before each surface.** After each round you show the user, commit in the
worktree so branch `HEAD` always equals what they are looking at:

```bash
git -C runtime/si-live/update-$SLUG add -A
git -C runtime/si-live/update-$SLUG commit -m "wip: <what this round changed>"
```

Then get the user's reaction -- a binary keep/keep-iterating plus room for
free-form notes -- and loop until they **explicitly confirm** the shape. That
confirmation is the gate to hardening; nothing heavy runs before it.

**A test-only / no-surface change** (e.g. a test-suite fix with nothing to look
at) skips the preview entirely: edit the worktree, commit, then go straight to
the harden handoff and safe-reveal below. Code isolation is still required --
every system-interface change runs through the worktree -- but there is no shape
to preview.

The worktree and preview **persist across turns**; if the user drifts away and
never approves, you release nothing automatically (no idle timeout) -- explicit
abandonment tears everything down (Step 4 teardown) and releases the lease.

## 3. On approval: hand off to a background harden worker on the same branch

Once the user approves the shape, hand the branch to a background worker that
runs the full test + review gate. This reuses the `update-artifact` orchestration
core (`artifact=system-interface`), with two system-interface deviations: the
worker is created **at approval, on the existing branch**, and the task frames
one of two handoff shapes.

**Free the branch first.** Git forbids the same branch checked out in two
worktrees, so before creating the worker you must release the lead's hold on
`mngr/update-$SLUG`:

```bash
# tear the live preview down and close its tab (it boots from the worktree)
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py unpreview --slug "update-$SLUG"
python3 scripts/layout.py close si-preview
# then remove the lead's worktree, freeing the branch for the worker
git worktree remove --force runtime/si-live/update-$SLUG
```

**Create the worker on the branch.** Follow `update-artifact` Steps 1-3 (open the
`update-$SLUG` tracking ticket, write the task file with `operation: update` /
`artifact: system-interface` frontmatter, launch, background-poll) with these
specifics:

- Launch with the **branch passthrough** so the worker checks out and *extends*
  the branch you built up, instead of branching anew from the served HEAD (which
  would lose your live commits):

  ```bash
  uv run .agents/skills/launch-task/scripts/create_worker.py launch \
      --name "update-$SLUG" --template subskill-worker \
      --runtime-dir "runtime/harden/update-$SLUG/" \
      --task-file "runtime/harden/update-$SLUG/task.md" \
      --branch "mngr/update-$SLUG"
  ```

  The worker re-syncs its own fresh worktree after launch, in the background,
  where nobody is waiting.

- **Task body = one of two handoff shapes:**
  - *Type 1 (janky real edit approved):* "the branch carries an approved but
    rough real edit -- implement the approved shape for real, then harden it."
  - *Harden-only (already-real-and-previewed, or committed-origin verify):* "the
    branch already carries the real, user-approved change -- verify and harden it;
    do not re-implement it."

  Per the system-interface exception in
  [`op-update.md`](../../shared/worker/references/op-update.md), there is **no
  `## Change origin` marker and no worker gate**: user approval already happened
  through your live loop. The worker implements/verifies per
  `artifact-system-interface.md`, runs the tests and review gates, and reports a
  plain `done` (or `question` / `stuck`). Include a `## Real scenario` section
  when a real conversation motivated the change -- name the motivating agent
  (usually your `$MNGR_AGENT_ID`) and describe in plain words what looked wrong,
  so the worker opens *that* conversation firsthand rather than reconstructing it
  from prose.

- **Terminal handling:** on `done`, go to Step 4. On `stuck` or a dead-worker
  timeout, surface to the user per
  [`worker-failure.md`](../launch-task/references/worker-failure.md) -- do not
  merge or reveal, and do not retry silently.

**Optional final preview before merge.** Keep a pre-merge preview when the worker
produced **real work the user has not seen** (the Type 1 janky -> real path: the
worker turned the approved rough edit into the real implementation) -- boot the
worker's already-built work_dir and let the user confirm the real version:

```bash
WORK_DIR=$(mngr ls --include 'name == "update-'"$SLUG"'"' --format json \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["agents"][0]["work_dir"])')
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview \
    --slug "update-$SLUG" --work-dir "$WORK_DIR"
python3 scripts/layout.py open si-preview
```

Because the system interface *is* the user's workspace, a final preview is
essentially always warranted when there is unseen real work (unlike an ordinary
service, where a tab refresh after go-live often suffices). It is optional only
when the user already previewed a polished, real version and the worker changed
nothing they would see. If the user rejects here, do not merge; tear the preview
down and decide *with them* whether to re-brief the worker.

## 4. Go live: freshness-check, merge, safe-reveal, tear down

With the worker `done` (and any final preview approved), merge and reveal. You
already hold the editing lease from Step 1, so no other chat's merge can
interleave.

1. **Freshness check** -- the branch is mergeable only if `apps/system_interface/`
   has not changed on the served branch since the worker branched:

   ```bash
   BASE=$(git merge-base HEAD "mngr/update-$SLUG")
   git diff --name-only "$BASE" HEAD -- apps/system_interface/
   ```

   Empty output means fresh -- continue. Any output means the pass is stale (some
   other change landed on the served tree); do **not** merge and never
   hand-resolve a conflicted merge (see
   [`harden-contention.md`](../../shared/references/harden-contention.md)).
   Re-brief the worker to rebase and re-verify, then come back.

2. **Capture the known-good revision** -- the served `HEAD`, *before* you merge.
   This is what the reveal rolls back to if the change breaks:

   ```bash
   ROLLBACK_TO=$(git rev-parse HEAD)
   ```

3. **Merge** `mngr/update-$SLUG` into the served working branch and commit the
   merge, so the tree is clean (the reveal refuses to run on a dirty tree). The
   built `static/` bundle is gitignored, so the merge brings only source and
   dependency-manifest changes; the reveal rebuilds the bundle.

4. **Reveal** with the captured revision:

   ```bash
   python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py reveal \
       --rollback-to "$ROLLBACK_TO"
   ```

   That single command owns the whole reveal as one deterministic, self-healing
   motion (you do not run `npm`/`uv`/`mngr` by hand). It classifies what changed;
   refreshes dependencies only if a manifest changed (`npm ci` / `uv tool install
   -e apps/system_interface --reinstall`); pre-flights a backend change on a
   throwaway port before touching the live service; rebuilds `static/` and
   broadcasts a reload (frontend) and/or restarts the services agent (backend);
   health-checks the live service; and auto-rolls-back to `--rollback-to` on any
   failure. Interpret the exit code and report it:

   - `0` -- revealed; the live UI is updated and healthy.
   - `2` -- the change was bad and was **automatically rolled back**; the live UI
     is healthy on the previous revision, but the requested change did **not**
     land. Diagnose before retrying.
   - `3` -- **emergency**: even rollback could not restore a healthy UI. Escalate
     immediately.
   - `1` -- precondition error (e.g. a dirty tree); nothing was changed.

   Why a script and not a checklist: if the backend fails to start, the user
   loses their entire chat UI -- there is nowhere left to surface an error. The
   recover-or-revert logic must run identically every time and can never be
   skipped.

5. **Tear down and release.** After a successful reveal (or after a rejection
   where nothing was merged), tear down any remaining preview and its tab,
   destroy the worker, close the ticket, and release the lease:

   ```bash
   python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py unpreview --slug "update-$SLUG"
   python3 scripts/layout.py close si-preview
   ```

   `unpreview` only handles the *service*; the `si-preview` tab is a layout panel
   you must close yourself, or the user is left with a stale tab pointing at a
   deregistered service. Then destroy the worker per `launch-task`, close the
   `update-$SLUG` ticket, and release the editing lease with
   `tk close "$LEASE_ID" "Live edit hardened, revealed, and torn down."`. Also
   remove the lead's worktree if it still exists (`git worktree remove --force
   runtime/si-live/update-$SLUG`).

## Why this shape

The UI is what the user is actively looking at, so a broken build must never be
served -- but that safety used to mean waiting out a full harden pass before the
user could see *anything*, inverting "live first, ratify at turn-end." The fix
keeps the safety (code isolation via the worktree, the health-checked
auto-rollback reveal) while restoring the fast loop: the lead edits and previews
in seconds, and the expensive gate runs once, in the background, only after the
user is happy with the shape. Preview boot, in-place refresh, and reveal are
deterministic, so they live as sub-commands of `reveal_system_interface.py`; the
only non-deterministic part -- gating on the user's judgment -- stays with you.
