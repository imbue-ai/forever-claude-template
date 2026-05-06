# Migrate minds workspace server into the template as `apps/system_interface`

## Overview

- Promote the minds workspace server to a first-class app in this template at `apps/system_interface/`, removing it from `mngr` (both `~/utilities/mngr` and the vendored copy at `vendor/mngr`).
- Folder name is renamed to match the existing `system_interface` service entry in `services.toml`; everything else (distribution name, CLI command, Python module path, env vars, class names) stays as-is for this PR. A deeper rename is a follow-up.
- Keep coupling to `mngr` intact: the server keeps importing `imbue.mngr.*`, `imbue.concurrency_group.*`, `imbue.imbue_common.*` and shelling out to `mngr observe`. Those deps continue to resolve through the vendored `vendor/mngr` workspace.
- The two repos are allowed to drift: this template's PR deletes only the vendored `vendor/mngr/apps/minds_workspace_server/` directory; the source-of-truth deletion in `~/utilities/mngr` lands in a separate PR on a fresh worktree.
- Frontend rebuild/refresh wiring is out of scope; built `static/` is gitignored and rebuilt on mind creation by the existing provisioning command (`cd apps/system_interface/frontend && npm ci && npm run build`).

## Expected behavior

- After this PR lands and a fresh mind is created from the template, the provisioning step builds the frontend (`cd apps/system_interface/frontend && npm ci && npm run build`) and installs the `minds-workspace-server` uv tool from `apps/system_interface/`. The bootstrap service manager then starts the `system_interface` service, which runs `minds-workspace-server` listening on `127.0.0.1:8000` and serves the freshly-built UI.
- The minds desktop client (in the still-extant `apps/minds` of `mngr`) talks to the workspace server over HTTP at `localhost:8000` exactly as before — no change to its behavior.
- The `minds-workspace-server` binary on PATH (from `uv tool install`) continues to work; resolves all `imbue.mngr.*`, `imbue.concurrency_group.*`, `imbue.imbue_common.*` imports correctly via path sources pointing at `vendor/mngr/libs/...`.
- Frontend changes still require the developer to manually run `npm install && npm run build` inside `apps/system_interface/frontend/`; no auto-rebuild or auto-refresh exists yet.
- In `~/utilities/mngr`, after the companion PR lands, `apps/minds_workspace_server/` and `specs/minds-workspace-server/` are gone, and there are no broken references in mngr's code, docs, or tests.
- `vendor/mngr` in this template will diverge from `~/utilities/mngr` (template-local deletion of one app directory). Subsequent `git subtree pull` operations need to be done with awareness; no documentation note is added for now.

## Changes

### In this template (forever-claude-template)

- `git mv vendor/mngr/apps/minds_workspace_server apps/system_interface`, with all internal contents preserved unchanged.
- Update `apps/system_interface/pyproject.toml`'s `[tool.uv.sources]`: replace `imbue-common = { workspace = true }` and `imbue-mngr = { workspace = true }` with `{ path = "../../vendor/mngr/libs/imbue_common", editable = true }` and `{ path = "../../vendor/mngr/libs/mngr", editable = true }` respectively. The `editable = true` is required because imbue-mngr's transitive resolution treats imbue-common as an editable workspace member, and uv refuses mixing editable + non-editable URLs for the same package.
- Keep the install pattern as `uv tool install` (matching how the vendored copy was installed). Do NOT add `apps/system_interface` to the template root's `tool.uv.workspace.members` and do NOT add `minds-workspace-server` to the root project dependencies. The CLI binary lands on PATH via `uv tool install` exactly as it did before the move.
- Update path references in:
  - `Dockerfile` (frontend build directory; `uv tool install -e ...` target).
  - `.mngr/settings.toml` `create_templates.dev.extra_provision_command` and `create_templates.lima.extra_provision_command` (frontend build directory; `uv tool install` target).
- `services.toml` requires no edits — the `system_interface` service entry already invokes `minds-workspace-server` and that CLI entry point is unchanged.
- Add `apps/README.md` explaining the `apps/` directory convention (parallel to `libs/`; first app introducing this directory).
- Move the spec from `~/utilities/mngr/specs/minds-workspace-server/concise.md` to `specs/system_interface/concise.md` in this template.
- Consolidate the moved app's frontend gitignore patterns (built `static/`, binary asset paths) into the template's root `.gitignore` and delete the local `apps/system_interface/.gitignore`. `node_modules/` is already covered by the root `.gitignore`.
- Manually verify: `uv tool install -e apps/system_interface` succeeds; `npm ci && npm run build` in `apps/system_interface/frontend/` produces assets in `apps/system_interface/imbue/minds_workspace_server/static/`; `minds-workspace-server` binary boots, serves `/api/agents`, and serves built static assets.

### In `~/utilities/mngr` (companion PR, separate worktree)

- Create a fresh worktree at a path outside the existing checkouts, branch `gabriel/remove-minds-workspace-server`.
- Delete `apps/minds_workspace_server/` in its entirety.
- Delete `specs/minds-workspace-server/`.
- Grep the rest of mngr for any references to the deleted app — READMEs, docs, scripts, e2e test comments — and remove or update them in the same PR.
- Run mngr's full test suite to confirm no remaining references break anything.

### Drift

- After both PRs land, `vendor/mngr` in this template lacks `apps/minds_workspace_server/` while `~/utilities/mngr` (post-deletion) also lacks it; in the in-between window where only the template PR has landed, the two diverge by exactly that one directory deletion.
- No documentation of this drift is added; future contributors performing a `git subtree pull` must reconcile manually.
