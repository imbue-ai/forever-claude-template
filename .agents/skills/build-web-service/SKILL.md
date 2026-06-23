---
name: build-web-service
description: "Use when you want to create a new web view for the user -- a page, dashboard, or app they can open as a tab. Runs an interactive flow: confirm the look and feel on a cheap throwaway mock first, then build the real site to a usable state, then harden it in the background. Covers scaffolding a new Flask service (canonical path) and the escape hatch for wrapping a pre-existing third-party server."
metadata:
  crystallized: true
---

# How to build a web service

A "web service" here is something the user can click on as a tab in
the desktop client and see render at `/service/<name>/`, proxied
through the system_interface.

There is one canonical path (scaffold a new Flask lib) and one
escape hatch (wrap a pre-existing third-party server). Modify/remove
flows go through the `edit-services` skill.

## This is the web specialization of the interactive-delivery shape

**Read `.agents/shared/references/interactive-delivery.md` first.** Building a web
view is not a "scaffold, implement, ship" recipe -- it is an *interactive* flow:
you confirm the look-and-feel on a cheap throwaway mock *before* building the real
thing, build to a usable state in the foreground, and defer the thorough
testing + review gates to a background worker. The phases below fill in that
shared skeleton for web work. The single biggest mistake this skill exists to
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

Ask only the questions that genuinely *block* -- a fork that is both genuinely
uncertain *and* expensive to reverse later. Most web views have none: default to
the simplest conventional choice and to a **single user**, state each default in
one line, and move on. Cheap-to-reverse choices (persistence, auto-reload vs.
reload-to-refresh, latest-only vs. history) are not P0 -- pick the obvious
default and let them surface during the mock loop or as a later follow-up
surface, where the user can react to something concrete rather than answer
"should this update on its own?" in the abstract.

If you *do* hit a real blocker, phrase it as the user-visible consequence that
motivates it -- never a technical term (this system serves non-technical users):
"should everyone see the same list?" not "do we need multi-tenancy?".

Record your stated defaults -- they are the architecture you build once, after
the mock converges. Do not build any of it yet. Then propose a small plan and
wait for approval.

## Decide which path applies

- **Authoring routes yourself** (the common case): use the Flask
  scaffolder in Step 1. The scaffolder picks correct defaults so most
  framework gotchas don't fire.
- **Wrapping a pre-existing third-party server** (Jupyter, Grafana,
  an `npx`-installed dashboard, anything with its own start command):
  skip the scaffolder, jump to "Escape hatch: wrap an existing server"
  below.

If you would otherwise scaffold a Flask lib whose only job is to
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
  beats clever. Avoid names already used in `supervisord.conf`
  (`web`, `system_interface`, etc. are reserved by the scaffolder).
- **Pick a free port.** `ss -tln` lists what's bound. The scaffolder
  picks the lowest free port at or above 8081 by parsing
  `supervisord.conf` and `runtime/applications.toml`; if you're choosing
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
uv run .agents/skills/build-web-service/scripts/scaffold_flask_lib.py \
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
- `--extra-dep`: repeatable. Add libraries beyond `flask`/`flask-sock`
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
- `libs/<package>/src/<package>/runner.py` -- sync Flask starter.
  Builds a `Flask` app and serves it with
  `werkzeug.serving.run_simple(..., threaded=True)`. It serves at `/`;
  the system_interface proxy handles the `/service/<name>/` prefixing,
  so no `root_path`/`ROOT_PATH` is needed.
- `libs/<package>/test_<package>_ratchets.py` -- standard ratchets at
  zero.
- `libs/<package>/README.md` -- one-line description.

What gets updated:

- Root `pyproject.toml` -- adds `<service-name>` to
  `[project].dependencies`, `libs/<package>` to
  `[tool.uv.workspace].members`, and `<service-name> = { workspace = true }`
  to `[tool.uv.sources]`.
- `supervisord.conf` -- appends a program block:

  ```ini
  [program:<name>]
  command=bash -c "python3 scripts/forward_port.py --url http://localhost:<port> --name <name> && uv run <name>"
  directory=/mngr/code
  autostart=true
  autorestart=true
  # plus rotated stdout/stderr logfiles under /var/log/supervisor/<name>-*.log
  ```

  The Flask app serves at `/` and needs no prefix env var: the
  system_interface proxy handles `/service/<name>/` prefixing (it
  rewrites absolute paths in served HTML and installs a scoped service
  worker that prepends the prefix to the page's own fetches). The
  `bash -c "..."` wrapper is required because supervisord runs commands
  directly (no shell) and this one chains `forward_port.py` with `&&`.

supervisord does not watch the config, so tell it to pick up the new
program, then confirm it is running:

```bash
supervisorctl reread && supervisorctl update
supervisorctl status <name>
```

If it isn't `RUNNING`, read its log
(`/var/log/supervisor/<name>-stderr.log`) or run
`supervisorctl tail <name> stderr`.

### Put a throwaway mock in front of the user (the confirmation gate; looped)

Scaffolding the service is fine before confirmation -- it is cheap and reversible.
**Building the real data layer or state architecture before the user confirms the
look-and-feel is the tripwire: do not.** Instead, serve a *throwaway mock* of the
proposed UI as a route inside the scaffolded service, so the user sees it as a
real tab and reacts to the actual look-and-feel.

This is skeleton phase 5 (the cheap throwaway artifact). Keep it disposable:

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

The user may respond to the mock with a request for functionality that requires updated backend support.
Your mocks should remain mostly frontend code but demonstrate how things would likely look and feel
once that updated backend code is implemented. Be careful to confirm that the user will be happy
with how things look and feel and approximately function prior to doing the heavy work of building out backend code.

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

Use **sync handlers** (`def`, not `async def`). Flask handlers are
sync `def`, and the starter runs on the threaded Werkzeug server
(`run_simple(..., threaded=True)`), so concurrent requests are handled
by separate threads -- no asyncio needed.

### Rendering HTML for a human

If your service renders HTML that a person will look at (anything
beyond a pure JSON API, a webhook receiver, or a transparent proxy of
a third-party tool), you must invoke the `frontend-design` skill **before**
writing the markup. Always do this before working on UI, regardless of the scope of the work.

Skip this step for routes that emit only JSON, only redirects, or that
serve an existing third-party UI through the escape hatch below --
there's no markup to design.

### Calling Claude from your service

If your service needs to call Claude (classify/summarize content, run a one-shot
agentic task, or launch a full agent), follow the `use-ai-integration` skill: it
picks the path (a keyed `litellm` call or the keyless `claude_p.py` helper),
covers the `claude -p` environment fix and the cost model, and saves you from
hand-rolling the call.

### Always surface the raw data and its source

When a view renders data *derived* from underlying records (a summary,
a reformatted list, extracted fields), include -- by default, without
the user asking -- a "view raw" control showing the original record
**rendered in its native format** (an HTML email as the rendered email,
not escaped source; JSON pretty-printed; markdown rendered -- the
faithful original minus your processing) plus, for records from an
external service, an "open in <source>" link back to the origin (e.g.
open the email in Gmail). When you render untrusted third-party HTML (a
raw email body is the common case), sandbox it -- a sandboxed `iframe`
or a sanitizer -- so the view can't run scripts or phone home via
tracking pixels.

This is the surfacing half of the preserve-and-surface principle
(CLAUDE.md): the derived view inevitably leaves gaps (a field the agent
didn't extract, a rendering it didn't anticipate), and the raw/source
affordance lets the user bridge them without waiting for a rebuild.
Design it in from the first version -- it depends on the data layer
having persisted the raw payload and source reference (see the
crystallize data-capture guidance), so confirm that's available and
flag it if it isn't. Keep it unobtrusive (a small per-record control,
not clutter) and don't call it out in chat -- always present, never
announced.

### File-path conventions

Two cases, two patterns:

- **Runtime state files** (caches, cursors, last-visit timestamps,
  JSON snapshots written and read across runs): use cwd-relative
  paths like `Path("runtime/<name>/...")`. The supervisord-managed
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
the code-guardian gates -- runs in a **background harden worker**, never in the
main agent. This is skeleton phase 7: the harden pass
(`.agents/shared/worker/references/harden-artifact.md`), here the **crystallize**
operation with the **service** artifact -- the scaffolded service is already on
disk and the user confirmed it live, so nothing needs reconstructing and there
are no worker gates.

**The trigger is an explicit confirmation on the *working* site -- never your own
sense that the code looks done.** Once the usable site is in front of the user,
ask a plain "this generally looks good?" and hand off only once they confirm by
exercising the real behavior. (The mock confirmed the UX *shape*; this confirms
the real *behavior* -- the point where deep changes actually surface, so
finalizing earlier risks hardening an architecture the user is about to
invalidate.)

Reading the confirmation signal:

- If the user keeps asking for changes, each one is a **cheap foreground
  iteration that resets the clock** -- you have run no gates or thorough tests
  yet, so pivots stay cheap. Do not hand off until their response is a
  confirmation rather than a change request.
- If the user starts asking for surface-level (cosmetic) tweaks, or pivots to a
  slightly unrelated task or follow-up, treat that as a sign the core is settled:
  still ask, but ground it -- "seems like we've got the core thing settled here
  -- good to lock it in?" -- rather than leaving it open-ended.
- Wait for an explicit confirmation rather than firing on a timeout or silence.
  The user is never blocked: they already hold the usable site.

On confirmation, **hand the confirmed service to the `crystallize-artifact`
skill with `artifact=service`.** It owns the rest -- the tracking ticket, the
task file (set `artifact: service`), launching the generic `harden-worker`,
polling, merging on `done`, and refreshing the tab after merge. Give it only:
the slug (the service name), and a task body naming the built lib path, the
service name, the URL segment, and what the service does. The generic worker
loads `harden-artifact.md` + `op-crystallize.md` + `artifact-service.md` and
reports `done` once its testing contract and the review gates pass; there is no
worker gate because the user already confirmed the live site.

The confirmed mock plus the confirmed working site remain the single source of
truth: if finalization changes the look-and-feel, re-confirm with the user before
calling the work done.

## Escape hatch: wrap an existing server

For pre-existing third-party tools, do not scaffold a lib. Add a
`[program:<name>]` block to `supervisord.conf` that runs
`forward_port.py` and then your existing start command. supervisord runs
commands directly (no shell), so wrap any command that chains with `&&`
in `bash -c "..."`:

```ini
[program:<name>]
command=bash -c "python3 scripts/forward_port.py --url http://localhost:<port> --name <name> && <existing_start_command>"
directory=/mngr/code
autostart=true
autorestart=true
```

Two valid shapes:

- **Inline** (preferred when one line fits):

  ```ini
  [program:docs-viewer]
  command=bash -c "python3 scripts/forward_port.py --url http://localhost:8090 --name docs-viewer && jupyter notebook --port 8090 --ip 127.0.0.1 --no-browser"
  directory=/mngr/code
  autostart=true
  autorestart=true
  ```

- **Wrapper script** (preferred for multi-step bootstrap or env exports):

  ```bash
  # scripts/run_<name>.sh
  #!/usr/bin/env bash
  set -euo pipefail
  python3 scripts/forward_port.py --url http://localhost:<port> --name <name>
  exec <existing_start_command>
  ```

  ```ini
  [program:<name>]
  command=bash scripts/run_<name>.sh
  directory=/mngr/code
  autostart=true
  autorestart=true
  ```

After editing `supervisord.conf`, run `supervisorctl reread &&
supervisorctl update` to start the new program.

The `forward_port.py` call MUST come first in the command -- the port
must be registered before the app starts listening, otherwise the
app-watcher races with the backend coming up.

For the full program schema and logging knobs, see the `edit-services`
skill.

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

If the workspace has Cloudflare tunneling configured, the service is also
reachable at a public URL -- with caveats about where that hostname lives and
why it isn't in `runtime/applications.toml`. See
[references/public-url.md](references/public-url.md).

## Cleanup

To remove a web service (drop the `applications.toml` entry, stop and unregister
the supervisord program, and revert the scaffolded lib), see
[references/cleanup.md](references/cleanup.md).
