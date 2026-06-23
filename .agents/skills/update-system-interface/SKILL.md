---
name: update-system-interface
description: Canonical flow for changing the system interface (the web workspace UI at apps/system_interface) -- its frontend (dockview shell, chat rendering, progress view) or backend (Flask server, agent discovery, layout ops). Use whenever the user wants to edit, fix, restyle, or add to the workspace UI / chat interface / dockview.
---

# Updating the system interface

`apps/system_interface` is the live web UI the user is looking at right now
(the dockview shell, the chat panels, the progress view). A broken build here is
served straight to the user, so you never edit the served copy directly: you
make every change in an **isolated worktree clone**, verify it builds and passes
there, and only merge it back into the served tree once it's known-good. This
skill is the single canonical path for that.

This is the **system-interface specialization of the generic artifact
lifecycle.** It reuses the generic update orchestration -- the task file, the
generic `harden-worker`, and the report poll -- from `update-artifact` with
`artifact=system-interface`, and adds the one thing the system interface needs
that no other artifact does: a `safe-reveal` go-live (pre-merge **preview**, then
a reveal-or-roll-back script). The worker/orchestration core is shared; the
preview and reveal are owned here.

## The hard rule

**Never edit the system-interface tree that is being served to the user.** Do
not run `Edit`/`Write` on files under `apps/system_interface/` in this (the
served) checkout, and do not rebuild or restart the live UI from uncommitted
edits here. Every change is made in a separate, isolated clone of the source,
built and tested there, and merged back only after it passes. The only things
you do to the served tree are committing the merge and running this skill's
`preview` / `reveal` / `unpreview` commands -- and `preview`/`unpreview` never
modify the served tree at all (they only boot throwaway servers against the
worker's separate, already-built work_dir, so even the pre-merge preview can't
reach what the user is looking at).

That isolated clone is the generic harden worker -- its own git worktree and
copy of the source, so a half-broken build can never reach the user. The worker
is just the mechanism for that safe, separate place to work.

## Flow overview

1. **Delegate** the change to the generic worker via the `update-artifact`
   orchestration core (`artifact=system-interface`). The worker follows
   `harden-artifact.md` + `op-update.md` + `artifact-system-interface.md`, which
   own how to build, test, and verify the change in isolation.
2. The **worker** implements + builds + tests it on its own branch, then reports
   `done` (the system interface emits **no gate** -- approval is your preview).
3. You **preview** the change *before merging*: the worker is a local
   worktree-agent in this container that already built its own work_dir, so one
   command boots that folder and serves it -- wrapped in a labeled "preview"
   frame -- for the user to click around. The user approves or rejects.
4. **On approval**, you **record the known-good revision, then merge** the
   worker's branch.
5. You **reveal** the merged change with one command (refresh dependencies,
   rebuild/restart as needed, verify the live UI is healthy, auto-rollback on
   failure), then **tear down the preview**. On rejection, you tear down the
   preview and hand back -- nothing is merged.

## 1-2. Delegate via the generic update orchestration

Follow `update-artifact` Steps 1-3 (open a ticket, write the task file, launch
the worker, background-poll the report) with these system-interface specifics:

- **Pick a slug** `$SLUG` for the change. The worker agent / branch is
  `update-$SLUG` / `mngr/update-$SLUG`; the runtime dir is
  `runtime/harden/update-$SLUG/`.
- **Task-file frontmatter:** `operation: update`, `artifact: system-interface`,
  plus the standard `lead_agent` / `finish_report_path`
  (`runtime/harden/update-$SLUG/reports/report.md`). Per the system-interface
  exception in `op-update.md`, there is **no `## Change origin` marker** -- the
  body is a plain change brief, not an absorb/verify incident.
- **Task-file body:** `## What to do` (the actual UI change the user asked for),
  `## Context` (which panel, desired behavior, constraints), and `## Success
  criteria` (what "done" looks like, plus the standing line: *follow the
  installed `harden-worker` sub-skill; it composes `harden-artifact.md`,
  `op-update.md`, and `artifact-system-interface.md` for how to run, test,
  verify, and what not to touch; report `done` only when its testing contract
  and the review gates all pass*).
- **Launch** with `--template subskill-worker` (installs the generic
  `harden-worker`) per `update-artifact` Step 3, then background-poll per
  `.agents/shared/references/lead-proxy.md`.
- **Terminal handling differs:** the system interface emits no gate, and on
  `done` you do **not** merge here (that is `update-artifact` Step 4's behavior
  for other artifacts). Instead, go to the preview below. On `stuck` or a
  dead-worker timeout, surface to the user per
  `.agents/skills/launch-task/references/worker-failure.md` -- do **not**
  preview, merge, or reveal, and do not retry silently.

## 3. Preview the change before merging

On a terminal `done`, show the user the change *before* merging. The worker's
work_dir is a folder it has **already built** (its `done` contract runs
`uv sync` + `npm run build`). The preview just boots that folder -- no fetch, no
re-checkout, no rebuild. **Do not destroy the worker yet** -- the preview serves
its work_dir in place, so the worker must stay alive until the user gives a
verdict.

First resolve the worker's work_dir, then boot it:

```bash
WORK_DIR=$(mngr ls --include 'name == "update-<slug>"' --format json \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["agents"][0]["work_dir"])')
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py preview \
    --slug update-<slug> --work-dir "$WORK_DIR"
```

This boots the worker's already-built instance on a free port with layout
persistence neutered (it reads the same agents, so the user's real conversations
render, but it cannot clobber the live `layout.json`), then boots a small wrapper
page that embeds it in a labeled "preview" frame. The user-facing `si-preview`
service points at that wrapper (the inner instance is registered separately as
`si-preview-app`), so the tab reads as a clearly-marked proposed change. It does
**not** merge, touch the served tree, or modify the worker's folder. Exit `0`
means the preview is up; a non-zero exit means it failed to boot (or the
work_dir was wrong / the worker was already destroyed) and tore itself down --
diagnose before retrying.

Open it as a tab and ask the user to explore:

```bash
python3 scripts/layout.py open si-preview
```

Then confirm with the user via `send-user-message`: a binary keep/discard *and*
room for free-form notes (what looks off, what they'd change). Wait for their
answer before doing anything else.

## 4. On approval: record known-good, then merge

If the user **approves** the preview:

1. **Capture the known-good revision first** -- the served branch's current
   `HEAD`, *before* you merge. This is what the reveal rolls back to if the
   change breaks:
   ```bash
   ROLLBACK_TO=$(git rev-parse HEAD)
   ```
2. **Merge** the worker's branch (`mngr/update-$SLUG`) into the working branch
   the live UI is served from. Commit the merge so the tree is clean (the reveal
   refuses to run on a dirty tree, so a rollback can never clobber unrelated
   work).

If the user **rejects**, do not merge. Tear down the preview (see the end of the
next section) and hand back with their feedback -- decide *with them* whether to
re-brief the worker for another pass. Re-briefing is your judgment, not an
automatic loop.

Note: the built `static/` bundle is gitignored, so the merge brings only source
and dependency-manifest (`pyproject.toml` / `package.json` / lockfile) changes,
not the worker's build output. The reveal step rebuilds it.

## 5. Reveal the change (after merge), then tear down the preview

Run the reveal sub-command with the known-good revision you captured:

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py reveal \
    --rollback-to "$ROLLBACK_TO"
```

That single command owns the whole reveal as one deterministic, self-healing
motion -- you do not run `npm`/`uv`/`mngr` by hand. It:

- **Classifies** what the merge changed (frontend source, frontend manifest,
  backend source, backend manifest).
- **Refreshes dependencies only if a manifest changed** -- `npm ci` for the
  frontend, `uv tool install -e apps/system_interface --reinstall` for the
  backend. This is essential: a plain restart does *not* re-resolve the
  editable-installed tool's dependencies, so a backend dependency addition would
  otherwise crash the service on restart.
- **Pre-flights a backend change** by booting the merged code on a throwaway port
  before touching the live service. If it can't boot, the live service is never
  restarted -- the UI never goes down.
- **Reveals**: rebuilds the gitignored `static/` bundle and broadcasts a
  `reload_system_interface` op so open browsers reload into the new assets
  (frontend); restarts the services agent so the editable backend re-imports the
  merged `.py` (backend).
- **Verifies** the live service is healthy by polling its loopback endpoint.
- **Auto-rolls-back on any failure**: restores the tree to `--rollback-to` as a
  forward revert commit, rebuilds/restarts from it, and re-confirms the UI is
  healthy.

Interpret the exit code and report it to the user:

- `0` -- revealed; the live UI is updated and healthy.
- `2` -- the change was bad and was **automatically rolled back**; the live UI is
  healthy on the previous revision, but the requested change did **not** land.
  Report this and diagnose before retrying.
- `3` -- **emergency**: even rollback could not restore a healthy UI. The
  interface may be down; escalate immediately.
- `1` -- precondition error (e.g. a dirty tree); nothing was changed.

Once you no longer need the preview (after a successful reveal, *or* after a
rejection where nothing was merged), tear it down:

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py unpreview --slug update-<slug>
```

`unpreview` kills both preview servers (the inner instance and the wrapper) and
deregisters both their services (there is no worktree to remove -- the preview
served the worker's folder in place). It is idempotent, so it is also the safe
way to clean up after a `preview` that failed partway. Once the preview is down,
the worker can be destroyed per `launch-task`. Close the `update-$SLUG` ticket
the orchestration opened.

Why this exists as a script and not a checklist: if the backend fails to start,
the user loses their entire chat UI -- there is nowhere left to surface an error
message. The recover-or-revert logic must therefore run identically every time
and can never be skipped, which is exactly what belongs in a deterministic script
rather than agent prose.

`scripts/layout.py refresh` (the `manage-layout` skill) is unrelated -- it only
reloads a single inner iframe/panel for arranging the workspace, not the
top-level page, so it does **not** reveal a system-interface code change.

## Why this shape

The UI is what the user is actively looking at, so the design goal is "never
serve a half-broken UI," not "iterate in place fast." The worker's isolated clone
(in-process testing, Playwright verification, review gates) makes it safe to
merge; the pre-merge preview lets the user actually click around the change
before anything lands -- and since the worker already built its own work_dir, the
preview just boots that folder in place rather than re-cloning or rebuilding; and
the reveal script's pre-flight, health probe, and autonomous rollback make it
safe to reveal in one motion. Preview setup and teardown are deterministic, so
they live as `preview`/`unpreview` sub-commands of the same script rather than as
agent prose -- the only non-deterministic part, gating on the user's judgment,
stays with you.
