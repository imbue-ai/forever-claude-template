# Build-web-service skill restructure

## Refined prompt

> you should review build-web-service and raise any objections you have with it. and let's spec out the full final appearance
> * Umbrella skill named `build-web-service` (singular); scope is creation-focused — covers scaffold-new (canonical) and a wrap-existing escape hatch, plus diagnostic references for when things misbehave; modify/remove flows fall to `edit-services` (not duplicated).
> * Description phrasing is generic and user-facing — "use when you want to create a new web view for the user" — not framework- or mechanism-specific.
> * One canonical scaffolder: FastAPI (Python lib in `libs/<package>/`); the wrap-existing case (third-party tools, non-Python servers) writes a services.toml entry directly with no scaffolded lib, avoiding a double-proxy hop.
> * Cross-flow gotchas (WebSockets, bind-host, redirects) live in a shared `references/cross-flow-gotchas.md`; flow-specific gotchas stay inline in their respective sections of SKILL.md.
> * Both scaffold-new and wrap-existing paths require curl + Playwright verification (shared `references/verify.md`).
> * `forward-port` skill is deleted; its `forward_port.py` CLI usage and `--remove` flag absorbed into `build-web-service`.
> * `expose-web-service` skill (this branch's contribution) is deleted; its gotchas content moves to `references/cross-flow-gotchas.md`, its verification content moves to `references/verify.md`, and its escape-hatch walkthrough collapses into a SKILL.md section.
> * `edit-services` skill is kept standalone — it's a primitive used by non-web services (telegram-bot, runtime-backup, etc.).
> * PR #19 bug fixes folded in: reserved-names mismatch fix (`system_interface` vs `system-interface`); per-service-scoped env vars instead of generic `WEB_SERVER_PORT`.
> * FastAPI scaffolder uses `ROOT_PATH` env var (default empty) read by the generated runner; services.toml command sets `ROOT_PATH=/service/<name>` so standalone `uv run <name>` keeps working at `/`.

## Overview

- Three skills today (`forward-port`, `expose-web-service`, PR #19's `build-web-service`) have overlapping triggers around "expose an HTTP service." Trigger ambiguity makes it unclear which to invoke when an agent decides "this needs a web view."
- Consolidate into a single skill `build-web-service` with one canonical happy path (scaffold a FastAPI lib) and a small escape hatch for wrapping pre-existing third-party tools. Diagnostic references (gotchas, verification) are shared between the two paths.
- The scaffolder is FastAPI-only on purpose. The "wrap an existing third-party tool" case writes a services.toml entry directly — no intermediary FastAPI proxy. This avoids a second proxy hop in the request path while still routing through the same gotchas/verification references.
- `edit-services` stays standalone. It's a true primitive used by non-web services (telegram-bot, runtime-backup, future cron-like jobs); folding it in would either duplicate its content or leave non-web callers without an entry point.
- Folding in PR #19 also fixes its existing latent bugs: the kebab/snake reserved-names mismatch (`system-interface` vs the actual `system_interface` entry); the workspace-wide `WEB_SERVER_PORT` env var that would collide if multiple scaffolded libs were started in the same shell; the missing FastAPI `root_path` configuration that today forces every agent through the absolute-URL/redirect gotcha.

## Expected behavior

- An agent who decides "I need a web view" finds exactly one matching skill (`build-web-service`). The two old aliases (`forward-port`, `expose-web-service`) no longer exist as triggerable skills.
- Default flow: agent runs the scaffolder, which generates `libs/<package>/` with a FastAPI starter, updates the root `pyproject.toml` workspace, adds a `[services.<name>]` entry, runs `uv sync --all-packages`. The bootstrap manager picks up the entry and starts the service automatically.
- The generated FastAPI lib is reachable at `/service/<name>/` through the workspace_server (and the corresponding Cloudflare URL if a tunnel token is configured) without further action. FastAPI emits prefix-aware absolute URLs because `root_path` is set via env var.
- The generated lib still runs cleanly standalone via `uv run <name>` for fast iteration — `ROOT_PATH` defaults to empty, so the app serves at `/` when not invoked through the bootstrap-managed services.toml command.
- Wrap-existing escape hatch: when an agent is wrapping a third-party tool (Jupyter, Grafana, Express dev server), they read the SKILL.md escape-hatch section and write a services.toml entry that runs `forward_port.py` + the third-party command directly — no `libs/<pkg>/` scaffold. The same gotchas and verification references apply.
- Verification step (curl + Playwright two-tier) is identical for both paths and runs against `http://127.0.0.1:8000/service/<name>/`.
- Diagnostic flow when something misbehaves (e.g. duplicated dockview tab bar, redirect loop, WebSocket failure): the agent loads `references/cross-flow-gotchas.md`, which is symptom-indexed and applies regardless of which path produced the service.
- Modifying or removing an existing service is unchanged: the agent uses `edit-services` for the toml mechanics; the SKILL.md mentions `forward_port.py --remove` for cleanup of `runtime/applications.toml` when removing a service.
- PR #19 bug fixes are observable: a `--name system_interface` invocation is now rejected (was previously accepted while the actual reserved entry uses snake_case); two scaffolded libs run side-by-side without env-var collision; FastAPI's `/openapi.json` and any absolute redirects emit prefix-correct URLs by default.

## Changes

### Skills consolidated

- Delete `.agents/skills/forward-port/`. Its CLI flag reference and `--remove` invocation move into a section of `build-web-service`'s SKILL.md (the wrap-existing escape hatch already calls `forward_port.py` directly).
- Delete `.agents/skills/expose-web-service/`. Its `gotchas.md` becomes the new `references/cross-flow-gotchas.md`; its verification step becomes `references/verify.md`; its escape-hatch walkthrough collapses into a SKILL.md section.
- Keep `.agents/skills/edit-services/` unchanged. The new `build-web-service` SKILL.md cross-links to it for services.toml schema details rather than duplicating.

### New top-level skill

- Create `.agents/skills/build-web-service/` (taking PR #19's name).
- `SKILL.md` is substantive (not a thin router): contains the upfront branch (scaffold vs. wrap-existing), the pre-flight (port/name selection), the generator invocation, and links to `references/*` for verification and gotchas. Description is generic ("use when you want to create a new web view for the user") so it triggers on user-intent phrasing, not framework-specific keywords.
- `scripts/scaffold_fastapi_lib.py` — PR #19's `run.py` with the bug fixes applied (reserved-names list aligned to the actual `services.toml` entries, per-service env-var naming, default `ROOT_PATH` env-var wiring in the generated runner).
- `references/cross-flow-gotchas.md` — symptom-indexed reference covering the dockview-bar fall-through, redirect/Location rewriting behavior, FastAPI `root_path` (now mostly redundant after the default change but documented for awareness), static-server trailing-slash handling, WebSocket scheme derivation, multi-port apps, port-already-in-use diagnostics. Loaded on demand when verification surfaces something unexpected.
- `references/verify.md` — the shared two-tier verification recipe (curl against `/service/<name>/`, then Playwright assertion on a unique-to-the-app marker). Loaded by both the scaffold-new and wrap-existing flows.

### Scaffolder behavior changes (PR #19 bugs)

- Reserved-names list updated to match the kebab-cased and snake-cased forms actually present in `services.toml` (and rejects both `system-interface` and `system_interface` for safety).
- Generated `runner.py` reads `ROOT_PATH` from env (default `""`) and passes it to `FastAPI(...)`.
- Generated services.toml command sets `ROOT_PATH=/service/<name>` inline so the prefix is applied only under the bootstrap-managed run.
- Generated `runner.py` reads its port from a per-service env var (`<UPPERCASE_NAME>_PORT` or just inlines the literal port — TBD, see Open Questions); the workspace-wide `WEB_SERVER_PORT` is dropped.
- SKILL.md documents the existing `--skip-uv-sync` flag (currently undocumented) for fast-iteration cases.
- Print message at the end of generation references the verification step (curl + Playwright via the new shared reference).

### Boundary with `edit-services`

- `build-web-service` SKILL.md does not duplicate the services.toml schema; it links to `edit-services` for that.
- The wrap-existing escape hatch tells the agent: "write a services.toml entry following the same pattern as the example `[services.web]`; see `edit-services` for schema." The agent doesn't need to read both skills end-to-end — the link is for reference, not handoff.

### Cleanup of pre-existing references

- Update any internal cross-references in other skills (`.agents/skills/*/SKILL.md`, `references/*.md`) that point at `forward-port` or `expose-web-service` to point at `build-web-service` instead. Audit before deleting.
- The skill list in CLAUDE.md (if it enumerates) updates to reflect the consolidation.

### Out of scope (deferred)

- Scaffolders for non-FastAPI frameworks (static-files, Node). Decided against — the wrap-existing path is sufficient and avoids cargo-culting.
- A "modify an existing web service" flow as a separate reference. Decided against — `edit-services` already covers the toml mechanics.
- Auto-removal of a scaffolded lib (delete `libs/<pkg>/`, revert root pyproject.toml diff, drop services.toml entry, drop applications.toml entry). The skill mentions the steps in prose; no automation. Could be added later if the lifecycle becomes painful.

## Resolved decisions (was: open questions)

- **Generated-runner port handling**: hardcode the literal port into the generated `runner.py`. Drop the `WEB_SERVER_PORT` env-var indirection entirely.
- **`forward-port` CLI doc placement**: inline in `build-web-service`'s SKILL.md. The CLI surface is small (3 flags + `--remove`); both flows reference it (the scaffolder generates the call, the wrap-existing flow has the agent type it).
- **Migration**: absorb PR #19's content fresh into the restructure branch — do not cherry-pick its 4 commits.
- **Gotchas distribution**: single `references/cross-flow-gotchas.md` with everything; do not split per-flow. The FastAPI scaffolder neutralizes most flow-specific gotchas anyway.
- **Inbound references audit**: grep for references to `forward-port` and `expose-web-service` from other skills, scripts, hooks, READMEs, and CLAUDE.md before deletion. Fix any dangling links as part of the same change.
