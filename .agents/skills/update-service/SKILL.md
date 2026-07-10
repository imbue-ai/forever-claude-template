---
name: update-service
description: "Use whenever you are about to change an existing service -- edit, fix, restyle, extend, or restart its backend or frontend logic, or change how it runs. Covers both user-facing web services (a tab the user can open) and background daemons (runtime-backup, cloudflared, and other supervisord programs with no tab). This is the front door for service edits: it owns the live change loop (apply the change so it takes effect, refresh the user's view, verify) and hands the change to the turn-end hardening flow. For creating a brand-new web view use build-web-service; for the workspace UI itself use update-system-interface."
---

# Changing an existing service

A "service" here is a `[program:<name>]` under supervisord (see
`supervisord.conf`). There are two kinds, and the flow differs only in
whether there's a tab to refresh:

- **User-facing web service** -- something the user opens as a tab and
  sees render at `/service/<name>/` (scaffolded via `build-web-service`).
  Changing it means the open tab is showing the *old* page until you
  refresh it.
- **Background daemon** -- a supervisord program with no tab
  (`runtime-backup`, `cloudflared`, forwarders, cron-like jobs). There's
  nothing to refresh; the change takes effect when the process restarts.

**This skill is the front door for editing a service's code.** You do not
need to have loaded it to start typing an edit -- but the moment you are
changing a service, this is the process to follow, because two things are
easy to forget and both leave the user looking at stale state: (1) the
change doesn't take effect until the process is reloaded, and (2) an open
web tab keeps showing the old page until it's refreshed.

If you are doing something other than editing an existing service, you are
in the wrong skill:

- **Creating a new web view** -> `build-web-service`.
- **Changing the workspace UI itself** (`apps/system_interface` -- the
  dockview shell, chat panels, progress view) -> `update-system-interface`
  (it never edits the served tree directly; it previews in isolation and
  reveals only when known-good).
- **Rearranging tabs** (split/move/focus/rename/close) -> `manage-layout`.

## Match the flow to the scope of the change

Not every change is a quick edit. Before you start, decide which of these
the request is -- it changes what you do *before* touching code:

- **Small / contained change** -- a bug fix, a copy tweak, a new field, a
  backend logic fix, a config or `command` change, a restart. Go straight
  to the live change loop below: edit, apply, refresh, verify.

- **Larger-scope change** -- a redesign, a new page or view, a meaningful
  shift in look-and-feel, or a new user-facing capability. This is not a
  quick edit; it is a fresh pass through the **interactive-delivery shape**
  -- the *same* flow `build-web-service` used to build the service in the
  first place. **Read `.agents/shared/references/interactive-delivery.md`**,
  then follow `build-web-service`'s mock-confirm loop: put a cheap,
  throwaway version of the *proposed* change in front of the user (render it
  against the service's real data/state where you can -- *reading* the live
  store to preview is fine; never let a preview or verification *write* to it,
  see "Protect the user's data while you verify" below), loop until they
  **explicitly confirm** the shape, and only then build the real thing to a
  usable state. Do not do the heavy build against an unconfirmed shape --
  the same tripwire the create flow exists to prevent applies to edits.

  When a hand-built mock won't convince -- a redesign, a data-touching change
  -- you can instead boot the *actually changed* service as a labeled preview
  tab beside the live one, via the shared `serve_isolated_instance.py` script
  (see "Protect the user's data while you verify" for the exact invocation).
  That is the same preview mechanism the system-interface flow uses; reach for
  it when the user needs to click the real thing, and keep the lighter hand
  mock for quick look-and-feel loops.

  A new view or capability bolted onto an existing service is its own
  delivery with its own feedback gate (interactive-delivery phase 8):
  confirm and ship it on its own rather than bundling it with unrelated
  changes.

When in doubt about which bucket you're in, treat a change that alters what
the user *sees or perceives* as larger-scope (confirm the shape first) and a
change that only alters behavior behind an unchanged surface as contained.
Either way, the mechanics of surfacing the change to the user are the same
live loop:

## The live change loop

Make the change interactive and keep the user's view in sync as you go.

### 1. Make the change

Edit the service's code under `libs/<package>/` (or wherever the program's
command points). If the change renders HTML a person looks at, invoke the
`frontend-design` skill before writing markup, and if it calls Claude,
follow `use-ai-integration` -- the same rules as when the service was
built.

### 2. Apply it so it actually takes effect

The scaffolded web runner runs with `use_reloader=False`, and daemons
don't watch their own source, so a code change is **not** live until the
process restarts:

- **Backend change** (Python / server logic, for a web service or a
  daemon): restart the program.

  ```bash
  supervisorctl restart <name>
  supervisorctl status <name>   # confirm it came back RUNNING
  ```

- **Frontend-only change** (templates, static JS/CSS served fresh on each
  request): no restart needed -- the next request already serves the new
  markup. Skip straight to the refresh.

- **Change to the service *definition*** (its port, its `command`, its log
  config, or adding/removing a program): edit `supervisord.conf`, then
  `supervisorctl reread && supervisorctl update`. The full program schema,
  the add/remove/inspect mechanics, and the `forward_port.py` wiring live
  in [`.agents/shared/references/service-processes.md`](../../shared/references/service-processes.md).

If it doesn't come back `RUNNING`, read
`/var/log/supervisor/<name>-stderr.log` or
`supervisorctl tail <name> stderr`.

### 3. Refresh the user's view (web services only)

If the service has a user-facing tab, the open iframe is still showing the
pre-change page. Refresh it so the user sees the update without being told
to click Refresh:

```bash
python3 scripts/layout.py refresh <name>
```

`refresh` reloads every iframe for the service. If no tab is open yet and
the change is ready to show, surface it instead with
`python3 scripts/layout.py open <name>`. For any other tab manipulation,
see `manage-layout`. Background daemons have no tab -- skip this step.

### 4. Verify

Confirm the change actually does the right thing, exercised as the user
would (not just "the process is up"):

- **Web service**: `curl` against
  `http://127.0.0.1:8000/service/<name>/` then a Playwright assertion on a
  marker unique to your change. The recipe is in
  `build-web-service`'s [verify reference](../build-web-service/references/verify.md);
  the symptom-indexed gotchas (502, duplicated tab bar, redirect loop,
  broken WebSockets) are in that skill's `cross-flow-gotchas.md`.
- **Daemon**: watch its log (`supervisorctl tail -f <name> stderr`) and
  confirm the new behavior actually fires.

### Protect the user's data while you verify

The service's persistent store -- `runtime/<name>/` (whatever `DATA_DIR`
resolves to) -- **is the user's real data**. The recurring, expensive
failure mode is not the code edit: it is *verifying* a change by writing
test data into the live store and then "cleaning up" with a delete/reset
whose predicate is too broad and takes real records with it. The delete is
where the data dies. Encode these, cheapest first:

- **Read-only verification needs no ceremony.** Most changes (UI, copy, a
  backend read path) can be exercised by curl/Playwright against the live
  service without writing anything. Reading the live store -- including to
  *render* a preview -- is fine; the danger is only writes.

- **If exercising the change must write, mutate, or delete data, never
  point it at the live store.** Copy the store to a scratch path *outside*
  `runtime/` (so it is neither served nor swept into runtime-backup), boot a
  throwaway instance against the copy on a *spare* port, exercise it there,
  then delete the *copy*. The shared
  [`serve_isolated_instance.py`](../../shared/scripts/serve_isolated_instance.py)
  script owns the boot + teardown -- it picks a free port, injects it (via the
  `<PACKAGE_UPPER>_PORT` override) plus your data-dir override, waits for the
  instance to answer, and prints its URL:

  ```bash
  cp -r runtime/<name> /tmp/<name>-scratch
  URL=$(python3 .agents/shared/scripts/serve_isolated_instance.py up \
      --name <name>-test --cwd . \
      --port-env <PACKAGE_UPPER>_PORT \
      --env <PACKAGE_UPPER>_DATA_DIR=/tmp/<name>-scratch \
      --health-path /health \
      -- uv run <name>)
  # ...exercise the change at "$URL" (curl / Playwright); it can write freely...
  python3 .agents/shared/scripts/serve_isolated_instance.py down --name <name>-test
  rm -rf /tmp/<name>-scratch      # deleting a copy can't harm real data
  ```

  This is the point of the `DATA_DIR` + `<PACKAGE_UPPER>_PORT` overrides: the
  isolation you need is **data isolation, not code isolation**, and it's a
  copy-plus-one-command setup, not a worktree. The live store is only ever
  *read* (once, to make the copy); the only delete lands on a disposable path
  where real data never lived. If you want the user to *see* the throwaway
  instance -- a redesign, or a risky change where a hand mock won't convince --
  add `--service-name <name>-preview-app --preview-service-name <name>-preview
  --preview-title "<change>"` to the `up` call to surface it as a labeled
  "preview" tab (open it with `python3 scripts/layout.py open <name>-preview`);
  that is the same machinery the system-interface flow uses. Use judgment on
  when that is worth it.

- **Never "clean up" test data by deleting from the live store.** If you
  did leave a stray test record in it, leave it -- an additive junk record
  is a far cheaper mistake than a broad delete. Better: don't write to the
  live store in the first place (use the copy above).

- **Snapshot before any genuinely in-place change to the real store.** If a
  change truly must rewrite the live store (a data migration you can't run
  on a copy), `cp -r runtime/<name> /tmp/<name>-pre-<change>` first, run the
  change, confirm the real data survived, and only then remove the snapshot.
  The snapshot is a *recovery net* -- do **not** turn it into a routine
  "wipe live and restore backup" step: overwriting a running service's store
  tears its state, and any real writes that landed during your test window
  are silently lost on restore.

- **Retrofit older services when you touch them.** A service that predates
  this convention hardcodes `runtime/<name>/` and its listen port at its call
  sites. Add both overrides the scaffold now emits, as part of your change, so
  the throwaway instance above works: the data-dir override
  `DATA_DIR = Path(os.environ.get("<PACKAGE_UPPER>_DATA_DIR", "runtime/<name>"))`
  (route reads/writes through it), and the port override
  `PORT = int(os.environ.get("<PACKAGE_UPPER>_PORT", "<assigned-port>"))`
  (bind `PORT` in `run_simple`, never a hardcoded literal). If you genuinely
  can't, fall back to read-only verification plus the snapshot net.

- **The copy isolates local state, not external effects.** Pointing at a
  data copy does not stop a test run from really posting to Slack, calling a
  remote API, or sending a message. Guard those separately (a dry-run flag,
  test credentials) -- the data copy only protects the local store.

## Removing a service

Dropping a service is the definition-level case of step 2: remove its
`[program:<name>]` block, `supervisorctl reread && supervisorctl update`,
and (for a web service) `python3 scripts/forward_port.py --name <name>
--remove` plus reverting the scaffolded lib. The mechanics are in
[`.agents/shared/references/service-processes.md`](../../shared/references/service-processes.md); for a
scaffolded web lib, `build-web-service`'s `cleanup.md` reference has the
full teardown.

Teardown stops at the code and the process. **Leave the service's data
(`runtime/<name>/`) in place** -- removing a service is not license to
delete the user's records. Delete the data dir only if the user explicitly
asks, and confirm before you do.

## Turn-end: harden the change

The live loop above delivers the change to the user interactively. At
turn-end, formalize it through the background worker pipeline -- the main
agent never runs the thorough test passes or the review gates itself. For a
larger-scope change, hand off only once the user has confirmed the *working*
result (not just the mock), exactly as `build-web-service`'s Step 5 gates on
the working site; a contained change can hand off as soon as it verifies.

- **A change you and the user discussed and applied live, or repeatable
  work you did by hand** -> invoke `update-artifact` with
  `artifact=service`. It opens a tracking ticket, dispatches the generic
  harden worker to verify/test the change on its own branch, proxies the
  gates, merges, and refreshes the tab on go-live.
- **The service errored or produced a wrong result and you worked around
  it** -> invoke `heal-artifact` (artifact = service) at turn-end instead.
- **The workspace UI (`apps/system_interface`)** -> `update-system-interface`
  owns its own preview-before-merge and safe-reveal go-live; use it rather
  than this flow.

`update-artifact` and `heal-artifact` also stand on their own as turn-end
skills; this skill's turn-end step is just the service-shaped entry into
them.
