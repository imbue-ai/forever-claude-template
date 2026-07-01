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
  against the service's real data/state where you can), loop until they
  **explicitly confirm** the shape, and only then build the real thing to a
  usable state. Do not do the heavy build against an unconfirmed shape --
  the same tripwire the create flow exists to prevent applies to edits.

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
  in [references/service-processes.md](references/service-processes.md).

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

## Removing a service

Dropping a service is the definition-level case of step 2: remove its
`[program:<name>]` block, `supervisorctl reread && supervisorctl update`,
and (for a web service) `python3 scripts/forward_port.py --name <name>
--remove` plus reverting the scaffolded lib. The mechanics are in
[references/service-processes.md](references/service-processes.md); for a
scaffolded web lib, `build-web-service`'s `cleanup.md` reference has the
full teardown.

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
