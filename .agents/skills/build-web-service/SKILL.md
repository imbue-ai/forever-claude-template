---
name: build-web-service
description: "Use when you want to create a new web view for the user -- a page, dashboard, or app they can open as a tab. Runs an interactive flow: confirm the look and feel on a cheap throwaway mock first, then build the real site to a usable state, then harden it in the background. Covers scaffolding a new FastAPI service (canonical path) and the escape hatch for wrapping a pre-existing third-party server."
metadata:
  crystallized: true
---

# How to build a web service

A "web service" here is something the user can click on as a tab in
the desktop client and see render at `/service/<name>/`, proxied
through the system_interface.

There is one canonical path (scaffold a new FastAPI lib) and one
escape hatch (wrap a pre-existing third-party server). Modify/remove
flows go through the `edit-services` skill.

## This is the web specialization of the interactive-delivery shape

**Read `.agents/shared/references/interactive-delivery.md` first.** Building a web
view is not a "scaffold, implement, ship" recipe -- it is an *interactive* flow:
you confirm the look-and-feel on a cheap throwaway mock *before* building the real
thing, build to a usable state in the foreground, and defer the thorough
testing + review gates to a background worker. The phases below bind that shared
skeleton's hooks for web work. The single biggest mistake this skill exists to
prevent is building (and testing, and hardening) a whole site before the user has
confirmed the basic shape is what they want.

Map of the flow:

- **Step 0 -- clarify and plan** (skeleton phases 1-3): blocking questions only,
  in business terms; a small plan; wait for approval.
- **Step 1 -- scaffold + throwaway mock** (skeleton phases 4-6): scaffold the
  service, put a mock UI in front of the user, loop to explicit confirmation of
  the look-and-feel. Hard gate.
- **Step 2-4 -- build to a usable site** (the existing build mechanics, run
  *after* confirmation): implement real routes, verify, surface the tab.
- **Step 5 -- finalize in the background** (skeleton phase 7): once the user
  confirms the *working* site looks right, hand thorough testing + the review
  gates to a background worker. The main agent never runs those itself.

If you were sent here by `fetch-process-show` for a web view over fetched data,
the data sample is already confirmed -- but you still run your own mock
confirmation here, because the data sample confirms the data *shape*, not the UI
shape. Render the handed-off `sample.json` in the mock so the user judges the UI
against real data.

## Step 0: Clarify and plan (business terms only)

Ask only the questions that block, and phrase **every** architectural choice as
the user-visible consequence that motivates it -- never as a technical term. This
system serves non-technical users; build-web-service exists precisely so you do
not ask them questions they cannot answer. Default to the simplest conventional
choice and to a **single user**, state the default in one line, and only ask when
a fork is both genuinely uncertain and expensive to reverse.

Worked examples (generate the actual questions per task -- this is not a fixed
list):

- Persistence: "Should what you do here still be saved when you come back
  tomorrow, or is it fine for it to reset each time?" -- not "flat file or
  database?".
- Multi-user: "Is this just for you, or will other people open the same page and
  need to see their own version?" -- not "do we need auth / multi-tenancy?".
- Freshness: "Should this update on its own, or only when you reload?" -- not
  "do we need websockets / polling?".
- History: "Do you want to look back at older ones, or only ever see the latest?"
  -- not "do we need a time-series store?".

Record the answers (and your stated defaults) -- they are the architecture you
build once, after the mock converges. Do not build any of it yet. Then propose a
small plan and wait for approval.

## Decide which path applies

- **Authoring routes yourself** (the common case): use the FastAPI
  scaffolder in Step 1. The scaffolder picks correct defaults so most
  framework gotchas don't fire.
- **Wrapping a pre-existing third-party server** (Jupyter, Grafana,
  an `npx`-installed dashboard, anything with its own start command):
  skip the scaffolder, jump to "Escape hatch: wrap an existing server"
  below.

If you would otherwise scaffold a FastAPI lib whose only job is to
shell out to a third-party tool, do not do that -- the system_interface
already proxies `/service/<name>/...` to whatever URL you register.
Adding a Python proxy in front of the third-party server adds a hop,
costs an extra process, and complicates WebSocket and streaming
behavior. Use the escape hatch instead.

Do not extend `libs/web_server/` to add a new view. That lib runs the
top-level workspace UI; new web views go in their own scaffolded lib
under `libs/<your-package>/` so they get an isolated tab and prefix.

## Pre-flight (both paths)

- **Pick a kebab-case service name.** Becomes the URL segment
  `/service/<name>/`. Short and descriptive (`news`, `docs-viewer`)
  beats clever. Avoid names already used in `services.toml`
  (`web`, `system_interface`, etc. are reserved by the scaffolder).
- **Pick a free port.** `ss -tln` lists what's bound. The scaffolder
  picks the lowest free port at or above 8081 by parsing
  `services.toml` and `runtime/applications.toml`; if you're choosing
  manually, avoid `8000` (system_interface) and `8080` (the example
  `web` service).
- **Bind to `127.0.0.1`** (not `0.0.0.0`). The forwarder reaches your
  app from inside the same container; binding to all interfaces is
  noise. The scaffolder does this. For the wrap-existing path, many
  Node frameworks default to `0.0.0.0` -- pass an explicit host
  (`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) if your
  third-party tool's default isn't loopback. Python defaults are
  usually loopback already.

## Step 1: Run the scaffolder (canonical path)

```bash
uv run .agents/skills/build-web-service/scripts/scaffold_fastapi_lib.py \
    --name <service-name> \
    --description "<one-liner>" \
    [--port <int>] \
    [--extra-dep <pkg>] [--extra-dep <pkg>] ...
```

Required:
- `--name`: kebab-case (lowercase letters/digits with single hyphens).
- `--description`: becomes the lib `pyproject.toml` description.

Optional:
- `--port`: explicit port; auto-picked if omitted.
- `--extra-dep`: repeatable. Add libraries beyond `fastapi`/`uvicorn`
  (e.g. `--extra-dep "jinja2>=3.1" --extra-dep "anthropic>=0.40"`).
- `--skip-uv-sync`: skip the final `uv sync --all-packages` (for fast
  iteration / dry runs).

The scaffolder fails non-zero with a clear stderr message if the lib
already exists, the name is reserved or invalid, the requested port
is taken, or `uv sync` fails.

What gets generated:

- `libs/<package>/pyproject.toml` -- declares
  `[project.scripts] <name> = "<package>.runner:main"`.
- `libs/<package>/src/<package>/__init__.py` -- empty.
- `libs/<package>/src/<package>/runner.py` -- sync FastAPI starter.
  Reads `ROOT_PATH` from env (default empty) and passes it to
  `FastAPI(...)` so the app emits prefix-aware URLs when reached
  through the proxy.
- `libs/<package>/test_<package>_ratchets.py` -- standard ratchets at
  zero.
- `libs/<package>/README.md` -- one-line description.

What gets updated:

- Root `pyproject.toml` -- adds `<service-name>` to
  `[project].dependencies`, `libs/<package>` to
  `[tool.uv.workspace].members`, and `<service-name> = { workspace = true }`
  to `[tool.uv.sources]`.
- `services.toml` -- inserts:

  ```toml
  [services.<name>]
  command = "ROOT_PATH=/service/<name> python3 scripts/forward_port.py --url http://localhost:<port> --name <name> && uv run <name>"
  restart = "on-failure"
  ```

  The `ROOT_PATH=/service/<name>` prefix is what makes FastAPI emit
  prefix-correct OpenAPI links and absolute redirects when reached
  through the system_interface. Standalone `uv run <name>` keeps
  working at `/` because the env var is unset there.

The bootstrap service manager picks up the new entry automatically
(no manual restart). Confirm with:

```bash
tmux list-windows | grep "svc-<name>"
```

If the window doesn't appear after a few seconds, capture the bootstrap
window to a file (`tmux capture-pane -t bootstrap -p > /tmp/bootstrap.txt`)
and read it -- do not pipe `tmux capture-pane` through `tail`/`head`,
since CLAUDE.md disallows that.

### Put a throwaway mock in front of the user (the confirmation gate)

Scaffolding the service is fine before confirmation -- it is cheap and reversible.
**Building the real data layer or state architecture before the user confirms the
look-and-feel is the tripwire: do not.** Instead, serve a *throwaway mock* of the
proposed UI as a route inside the scaffolded service, so the user sees it as a
real tab and reacts to the actual look-and-feel.

This is the cheap-throwaway-artifact hook (skeleton phase 5). Keep it disposable:

- The mock renders **static / hard-coded content** that demonstrates the proposed
  layout and interactions -- no real fetching, no persistence, no backend logic.
  Invoke the `frontend-design` skill before writing the markup (see Step 2).
- If you were handed a confirmed `sample.json` (the `fetch-process-show` hybrid),
  render *that real data* in the mock so the user judges the UI against real
  content. Otherwise use representative placeholder data that covers the shapes
  the real view will show (including an empty state and a busy/overflow state).
- `layout.py open <name>` to surface it (see Step 4 for the command), then loop:
  present -> take feedback -> update the mock so the change is *visible* ->
  re-present. Do not accept feedback and move on having only asserted you'll apply
  it.
- Loop until the user **explicitly confirms** the look-and-feel is right.

**Hard gate (skeleton phase 6).** Do not implement real routes, data, or state
(Step 2 onward) until that confirmation. The mock is the single source of truth
for the UI shape: if later work changes the look-and-feel, re-confirm before
calling the site done.

For the **escape-hatch path** (wrapping a third-party tool) there is no markup you
author, so there is no mock to build -- the demonstration is the wrapped tool
itself. Stand it up, show it to the user, and confirm it's what they wanted before
investing in configuration or integration around it.

## Step 2: Build the real routes to a usable site (after confirmation)

Everything from here runs **only after** the user has confirmed the mock. The
goal of the foreground work is a *usable* site the user can actually try -- not a
fully hardened one. Implement the real routes (replacing the mock), wire in the
data/state architecture you recorded in Step 0, run the Step 3 smoke verify, and
surface the tab (Step 4). Then **stop and hand the running site to the user** --
the thorough testing and review gates happen in the background (Step 5), not here.

The starter `runner.py` has just `GET /` (a placeholder HTML page)
and `GET /health` (returns `{"status": "ok"}`). Replace the
placeholder with your real routes.

Use **sync handlers** (`def`, not `async def`). The starter is fully
sync and most pages don't need otherwise.

### Rendering HTML for a human

If your service renders HTML that a person will look at (anything
beyond a pure JSON API, a webhook receiver, or a transparent proxy of
a third-party tool), you must invoke the `frontend-design` skill **before**
writing the markup. Always do this before working on UI, regardless of the scope of the work.

Skip this step for routes that emit only JSON, only redirects, or that
serve an existing third-party UI through the escape hatch below --
there's no markup to design.

### Always surface the raw data and its source

When a view renders data *derived* from underlying records (a summary,
a reformatted list, extracted fields), include -- by default, without
the user asking -- a clean affordance to see the raw record the view
was built from and/or jump to its source. Concretely: a "view raw"
control that shows the original record **rendered in its native format
-- an HTML email as the rendered email (not escaped HTML source text),
JSON pretty-printed, markdown rendered. The point is the faithful
original minus your processing, presented as a human would actually
read it.** When you render untrusted third-party HTML (a raw email
body is the common case), sandbox it -- a sandboxed `iframe` or a
sanitizer -- so the view can't run scripts or phone home via tracking
pixels. And, when the record came from an external service, an "open in
<source>" link back to the origin (e.g. open the email in Gmail).
This is the surfacing half of the preserve-and-surface principle in
CLAUDE.md: the derived view inevitably leaves gaps (a field the agent
didn't extract, a rendering it didn't anticipate), and a raw/source
affordance lets the user bridge that gap immediately instead of waiting
for a rebuild. Design it in from the first version -- it depends on the
data layer having persisted the raw payload and source reference (see
the crystallize data-capture guidance), so confirm that's available and
flag it if it isn't. Keep it unobtrusive (a small per-record control,
not clutter), and don't call it out in your chat messages -- it should
just be there for the user who goes looking. Always present, never
announced.

### File-path conventions

Two cases, two patterns:

- **Runtime state files** (caches, cursors, last-visit timestamps,
  JSON snapshots written and read across runs): use cwd-relative
  paths like `Path("runtime/<name>/...")`. The bootstrap-managed
  services run from `/mngr/code` (repo root), so this resolves
  consistently. Do NOT use `Path(__file__)`-based paths for runtime
  state.
- **Static assets shipped alongside the .py file** (templates,
  default configs, bundled JSON): `Path(__file__).parent / "assets/..."`
  is the right pattern.

## Step 3: Verify

Both paths use the same verification recipe. See
[references/verify.md](references/verify.md) -- curl against
`http://127.0.0.1:8000/service/<name>/` then a Playwright assertion
on a unique-to-your-app marker.

If verification surfaces something unexpected (502, "duplicated
dockview tab bar", redirect loop, broken WebSockets), see
[references/cross-flow-gotchas.md](references/cross-flow-gotchas.md)
-- it's symptom-indexed.

## Step 4: Surface the view to the user

Once verification passes, tell the workspace UI to actually open the
new tab. Without this step the user would have to discover it via the
"+" dropdown -- skip the surfacing step only for services with no UI
(pure JSON APIs, webhook receivers, etc.).

```bash
python3 scripts/layout.py open <name>
```

`layout.py` POSTs to a loopback-only workspace_server endpoint that
broadcasts a `layout_op` message over its WebSocket. The frontend
focuses the panel if a tab for `<name>` is already open, otherwise
splits a new iframe alongside the primary chat (60% web / 40% chat).
The script briefly waits for the service to appear in
`runtime/applications.toml` so it's safe to run immediately after the
`forward_port.py` call.

To force a reload of an already-open tab (e.g. after redeploying the
service) without prompting the user to click Refresh:

```bash
python3 scripts/layout.py refresh <name>
```

You should always `refresh` services after making changes, to make sure the user can see the updates.

For anything beyond `open` / `refresh` -- splitting, moving, focusing,
renaming, maximizing, replacing an iframe's URL, inspecting the live
tree -- see the `manage-layout` skill. `layout.py list` is also useful
when the user is asking about what tabs are available (it prints every
user-facing registered service plus every mngr-level agent, with
open/running flags; the workspace chrome's own `system_interface` entry
is hidden).

## Step 5: Finalize in the background (after the user confirms the working site)

The foreground work stops at a usable, surfaced site. The thorough pass --
extending Playwright coverage, the full test suite and ratchets, `/autofix`, and
the code-guardian gates -- runs in a **background finalization worker**, never in
the main agent. This is the harden-ratify hook (skeleton phase 7).

**The trigger is an explicit confirmation on the *working* site -- never your own
sense that the code looks done.** Once the usable site is in front of the user,
ask a plain "this generally looks good?" and only spawn the worker once they
confirm. Agent-judged completeness is not the signal; the user exercising the real
behavior and approving it is. (The mock confirmed the UX *shape*; this confirms
the real *behavior* -- the point where deep changes actually surface, so
finalizing earlier risks hardening an architecture the user is about to
invalidate.)

Reading the confirmation signal:

- If the user keeps asking for changes, each one is a **cheap foreground
  iteration that resets the clock** -- you have run no gates or thorough tests
  yet, so pivots stay cheap. Do not spawn the worker until their response is a
  confirmation rather than a change request.
- If the user starts asking for surface-level (cosmetic) tweaks, or pivots to a
  slightly unrelated task or follow-up, treat that as a sign the core is settled:
  still ask, but ground it -- "seems like we've got the core thing settled here
  -- good to lock it in?" -- rather than leaving it open-ended.
- Wait for an explicit confirmation rather than firing on a timeout or silence.
  The user is never blocked: they already hold the usable site.

On confirmation, spawn the worker via the `launch-task` mechanics, with two
specifics:

- **Launch with `--template subskill-worker`** (not the default `worker`). That
  template installs the bundled `build-web-service-worker` sub-skill into the
  worker's `.agents/skills/` tree.
- **Keep the task brief short and point it at the sub-skill** -- you do not
  restate how the worker tests or hardens anything; that lives in the sub-skill.
  The brief needs only: which service was built (lib path, service name, the URL
  segment), what it does, and the standing line *follow the
  `build-web-service-worker` sub-skill for how to test, harden, verify, and what
  not to touch; report `done` only when its testing contract and the review gates
  all pass.*

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name finalize-<service-name> \
    --template subskill-worker \
    --runtime-dir runtime/launch-task/finalize-<service-name>/ \
    --task-file runtime/launch-task/finalize-<service-name>/task.md
```

Then background-poll the report per `launch-task` (its Step 3-4 and
`.agents/shared/references/lead-proxy.md`): on `done`, merge the worker's branch;
on `stuck` or a dead-worker timeout, surface to the user -- do not retry silently.
The confirmed mock plus the confirmed working site remain the single source of
truth: if finalization changes the look-and-feel, re-confirm with the user before
calling the work done.

**`build-web-service` is invocation-agnostic.** It behaves identically whether
invoked directly, from `do-something-new`, or from `fetch-process-show`. It never
reaches back to a `crystallize-task` worker -- that coupling lives only in the
data flow. Its own hardening always goes through the finalization worker above.

## Escape hatch: wrap an existing server

For pre-existing third-party tools, do not scaffold a lib. Add a
`services.toml` entry that runs `forward_port.py` and then your
existing start command:

```toml
[services.<name>]
command = "python3 scripts/forward_port.py --url http://localhost:<port> --name <name> && <existing_start_command>"
restart = "on-failure"
```

Two valid shapes:

- **Inline** (preferred when one line fits):

  ```toml
  [services.docs-viewer]
  command = "python3 scripts/forward_port.py --url http://localhost:8090 --name docs-viewer && jupyter notebook --port 8090 --ip 127.0.0.1 --no-browser"
  restart = "on-failure"
  ```

- **Wrapper script** (preferred for multi-step bootstrap or env exports):

  ```bash
  # scripts/run_<name>.sh
  #!/usr/bin/env bash
  set -euo pipefail
  python3 scripts/forward_port.py --url http://localhost:<port> --name <name>
  exec <existing_start_command>
  ```

  ```toml
  [services.<name>]
  command = "bash scripts/run_<name>.sh"
  restart = "on-failure"
  ```

The `forward_port.py` call MUST come first in the command -- the port
must be registered before the app starts listening, otherwise the
app-watcher races with the backend coming up.

For schema details on `services.toml`, see the `edit-services` skill.

Verification and gotchas references apply identically to this path.

## `forward_port.py` CLI reference

Used by both paths (the scaffolder generates the call; the escape
hatch has you write it directly).

```
python3 scripts/forward_port.py --name NAME --url URL
python3 scripts/forward_port.py --name NAME --remove
```

Flags:

- `--name`: application name (must match the URL segment a user
  clicks: `/service/<name>/`).
- `--url`: full URL where the app is reachable from inside the
  container (e.g. `http://localhost:8090`).
- `--remove`: remove the named entry from
  `runtime/applications.toml`. Use this when tearing down a service.

## The global (Cloudflare) URL

If the workspace has Cloudflare tunneling configured, the service is
also reachable at a public URL in addition to the local one. Two
caveats:

- **The public hostname is owned server-side**, not by the
  cloudflared process running in this container. Skimming
  `svc-cloudflared`'s tmux output will not surface a URL.
- **The public URL is *not* written into `runtime/applications.toml`.**
  `forward_port.py` only stores `name` and `url` (the local
  `http://localhost:<port>` backend address). Do not grep that file
  for a public URL.

The reliable way to get the public URL is through the desktop client
itself: when the user clicks the service tab, the client resolves the
public hostname via its services API. If you need the exact URL for
testing, ask the user to read it from their browser's address bar.

If the workspace does not have a tunnel token configured, this section
does not apply -- the local `http://127.0.0.1:8000/service/<name>/`
URL is the only entry point.

## Cleanup

Removing a web service:

1. `python3 scripts/forward_port.py --name <name> --remove` (drops the
   entry from `runtime/applications.toml`).
2. Drop `[services.<name>]` from `services.toml` (use `edit-services`
   for guidance on the toml mechanics).
3. If you scaffolded a lib, also: `rm -rf libs/<package>/` and revert
   the matching diff in the root `pyproject.toml` (drop from
   `[project].dependencies`, `[tool.uv.workspace].members`, and
   `[tool.uv.sources]`).
