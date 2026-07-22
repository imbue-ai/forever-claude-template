---
name: update-service
description: "Use immediately whenever the user asks you to update, change, fix, restyle, extend, restart, or otherwise modify an existing service -- load this BEFORE touching the service's code. Applies to any change to a service's backend or frontend logic, or how it runs. Covers both user-facing web services (a tab the user can open) and background daemons (runtime-backup, cloudflared, and other supervisord programs with no tab). This is the front door for service edits: it owns the live change loop (apply the change so it takes effect, refresh the user's view, verify) and hands the change to the turn-end hardening flow. For creating a brand-new web view use build-web-service; for the workspace UI itself use update-system-interface."
---

# Changing an existing service

A "service" here is a `[program:<name>]` under supervisord (see
`supervisord.conf`). Two kinds, differing only in whether there's a tab to
refresh:

- **User-facing web service** -- the user opens it as a tab rendering at
  `/service/<name>/` (scaffolded via `build-web-service`).
- **Background daemon** -- a supervisord program with no tab
  (`runtime-backup`, `cloudflared`, forwarders, cron-like jobs).

Two things are easy to forget when editing either, and both leave the user
looking at stale state: a code change doesn't take effect until the process
is reloaded, and an open web tab keeps showing the old page until it's
refreshed. The live change loop below handles both.

If you're doing something *other* than editing an existing service:

- **Creating a new web view** -> `build-web-service`.
- **Changing the workspace UI itself** (`apps/system_interface` -- the
  dockview shell, chat panels, progress view) -> `update-system-interface`,
  this flow's system-interface specialization: it runs the same live loop, but
  against an isolated worktree (never the served tree), with the preview tab as
  the user's view, and reveals only when known-good.
- **Rearranging tabs** (split/move/focus/rename/close) -> `manage-layout`.

## Match the flow to the scope of the change

Not every change is a quick edit. Before you start, decide which of these
the request is -- it changes what you do *before* touching code:

- **Small / contained change** -- a bug fix, a copy tweak, a new field, a
  backend logic fix, a config or `command` change, a restart. Go straight
  to the live change loop below: edit, apply, refresh, verify.

- **Larger-scope change** -- a redesign, a new page or view, a meaningful
  shift in look-and-feel, or a new user-facing capability. Run the *same*
  mock-confirm flow `build-web-service` used to create the service: **read
  `.agents/shared/references/interactive-delivery.md`**, put a cheap,
  throwaway version of the *proposed* change in front of the user, loop until
  they **explicitly confirm** the shape, and only then build the real thing
  to a usable state. Never build heavy against an unconfirmed shape.

  This is the demonstrative-artifact choice from
  [`interactive-delivery.md`](../../shared/references/interactive-delivery.md)
  phase 5, in service terms. A lighter **hand mock** (Type 2 -- a detached
  throwaway) is fastest for quick look-and-feel loops. When it won't convince --
  a redesign, or a data-touching change -- boot the *actually changed* service as
  a labeled preview tab beside the live one (Type 1 -- the real edit shown
  through the real surface) via the shared `serve_isolated_instance.py` script
  (invocation under "Protect the user's data while you verify"; it's the same
  preview mechanism the system-interface flow uses). Either way, *reading* the
  live store to render a preview is fine; never let a preview or verification
  *write* to it.

  **Does this change warrant a preview at all?** Use judgment: a change to a
  surface the user perceives usually does; a behavior-only change behind an
  unchanged surface often does not (a tab refresh after go-live suffices). Don't
  stand up a preview tab for a change nobody needs to look at.

  A new view or capability bolted onto an existing service is its own
  delivery with its own feedback gate (interactive-delivery phase 8):
  confirm and ship it on its own rather than bundling it with unrelated
  changes.

When in doubt about which bucket you're in, treat a change that alters what
the user *sees or perceives* as larger-scope (confirm the shape first) and a
change that only alters behavior behind an unchanged surface as contained.
Either way, the mechanics of surfacing the change to the user are the same
live loop:

## One editor at a time: the service lease

Every chat shares this one working tree and this one live process, so two
agents editing the same service at the same time interleave destructively --
there is no merge step where that could be reconciled. Concurrent edits must
be *serialized*, and the serialization token is an advisory lease held as a
regular `tk` ticket (regular tickets are visible across agents).

**Pre-flight, before touching the service's code or config:**

1. Check whether another agent is mid-edit:

   ```bash
   tk ready > /tmp/service-leases.txt
   grep "editing service <name>" /tmp/service-leases.txt
   ```

   If a lease exists and `tk show <id>` says it is not yours, do **not**
   silently proceed: tell the user another chat is currently modifying this
   service and let them decide (wait, or explicitly override). If the holder
   looks abandoned -- its agent is no longer running, or the lease is hours
   old with no notes -- say that too and offer to break it. The lease is
   advisory: it is broken deliberately by the user's call, never silently.

2. Take your own lease:

   ```bash
   LEASE_ID=$(tk create "editing service <name>" -t chore \
       -d "Held by $MNGR_AGENT_NAME while editing this service; released at turn end.")
   ```

   then `tk start "$LEASE_ID"` (as its own command).

**Release the lease at the end of every editing turn** with
`tk close "$LEASE_ID" "Done editing for this turn."` -- never hold it across
an idle wait for user feedback. On the next feedback round, take a fresh
lease before editing again. Between turns the changes are committed, so
another chat editing sequentially on top is safe; only *simultaneous* editing
needs the lease.

**An in-flight harden pass does not block you -- the foreground wins.** If
`tk ready` also shows an in-progress `update <name>` or `heal <name>` ticket
(a background pass hardening an earlier change to this service), proceed with
your edit; your change simply makes that pass stale. Leave a note on that
ticket (`tk add-note <id> "..."`) so its owner coalesces at merge time. The
full contention rules live in
[`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md).

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

## Turn-end: get feedback, then harden the change

The live loop above delivers the change to the user interactively. At
turn-end, formalize it through the background worker pipeline -- the main
agent never runs the thorough test passes or the review gates itself.

**Get the user's feedback before you start any hardening pass.** Delivering
the change live is not the same as the user *wanting* it, so never dispatch
the hardening worker in the same turn you make the change. This holds for
*every* change, contained or larger-scope -- even a one-line copy tweak gets
shown and confirmed first. It can take **several rounds**: treat each
response as another live iteration -- make the change, show it, ask again --
and hold the harden pass until you are sure the user is satisfied. A single
"looks fine" mid-thread while they're still tweaking isn't done. (For a
larger-scope change this gate is the *working* result, not just the mock,
exactly as `build-web-service`'s Step 5 gates on the working site.)

- **A change you and the user discussed and applied live, or repeatable
  work you did by hand** -> invoke `update-artifact` with
  `artifact=service`. It opens a tracking ticket, dispatches the generic
  harden worker to verify/test the change on its own branch, proxies the
  gates, merges, and refreshes the tab on go-live.
- **The service errored or produced a wrong result and you worked around
  it** -> invoke `heal-artifact` (artifact = service) at turn-end instead.
- **The workspace UI (`apps/system_interface`)** -> `update-system-interface`
  owns its own live preview loop and safe-reveal go-live; use it rather
  than this flow.

`update-artifact` and `heal-artifact` also stand on their own as turn-end
skills; this skill's turn-end step is just the service-shaped entry into
them.

Both flows enforce single-flight per artifact: if another chat already has a
harden pass in flight for this service, they leave a note on its ticket
instead of dispatching a sibling, and the eventual superseding pass covers
both changes. See
[`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md).
