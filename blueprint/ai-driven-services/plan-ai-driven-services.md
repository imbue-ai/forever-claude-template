# Plan: `build-ai-service` skill (AI-driven services & AI integrations)

A new full skill that teaches an agent how to wire Claude into a service correctly: how to
pick among three integration patterns, how to credential each so it never starves the
interactive chat, and a small importable utility for the common operations. Names
(`build-ai-service` skill, `ai_service_helpers` lib) are informative-not-cheeky placeholders —
adjust if you prefer.

> Refined request:
> Create a full skill in this template for setting up AI-driven services and building AI
> integrations into services.
> * Cover three integration patterns and a decision tree the authoring agent runs at design
>   time to pick one:
>   * **Pattern 1 — full agent**: service launches a complete agent (e.g. to edit itself on
>     user feedback or fix an error) via `mngr create` → wait → apply → destroy; reuse/extend
>     `launch-task`. Always user- or error-triggered, never autonomous polling.
>   * **Pattern 2 — many one-shot agentic tasks** (e.g. "read email then act"): headless
>     Claude Code (`claude -p` / `mngr uncapped-claude`), which draws from the separate
>     programmatic pool, not interactive chat.
>   * **Pattern 3 — no agency needed**: direct Anthropic API if a key is available, else fall
>     back to headless `claude -p`.
> * Resolve the credentialing concern: `claude -p` sometimes fails to authenticate; reconcile
>   how mngr stores creds vs what `claude -p` expects, and normalize it in a utility.
> * Provide importable utility implementations of the common operations (e.g. the
>   API-key-with-`claude -p`-fallback helper) so services don't reimplement them.
> * Account for the recent (June 15, 2026) split: `claude -p` / Agent SDK usage draws a
>   separate subscription pool from interactive chat, so patterns 2/3 cannot block core chat;
>   the live concern is cost (finite programmatic credit, then full API rates) and the
>   `ANTHROPIC_API_KEY`-means-full-API-billing footgun.
> * Pinpoint and fill the gaps in the `launch-task` dispatcher needed for a synchronous
>   create → wait → collect-result → destroy flow.

## Overview

- **Three integration patterns, one decision tree.** The skill's core is a short design-time
  procedure:
  - **Pattern 1 — full agent**: existing `launch-task` machinery (`mngr create` → wait →
    merge/apply → destroy). User-triggered or error-triggered only.
  - **Pattern 2 — many one-shot agentic tasks**: headless Claude Code (`claude -p` /
    `mngr uncapped-claude`) — draws the separate programmatic pool, not interactive chat.
  - **Pattern 3 — no agency**: direct Anthropic API if a key is available; else fall back to
    headless `claude -p`.
- **Billing is the organizing principle, and the facts changed recently.** As of the
  June 15, 2026 split, subscription usage is two pools: *interactive* (chat, terminal Claude
  Code, Cowork) and *programmatic / Agent SDK* (`claude -p`, Agent SDK, GH Actions). `claude -p`
  no longer competes with interactive chat for quota — it draws the separate programmatic credit
  (finite, then full API rates). With `ANTHROPIC_API_KEY` set, headless calls bill direct API.
  Either way, patterns 2/3 cannot block core chat. The live concern is cost, so the skill makes
  the active billing path explicit and logged rather than incidental.
- **Resolve the auth footgun the user flagged.** Headless `claude -p` "sometimes fails to
  credential" because bare `claude` reads the *default* `~/.claude`, while mngr provisions a
  *per-agent* `CLAUDE_CONFIG_DIR` with synced creds and (inside an agent) sets
  `ORIGINAL_CLAUDE_CONFIG_DIR=~/.claude` — which can point credential lookup at an empty dir.
  A service that inherits the running agent's environment usually has working creds; failures come
  from a stripped env or that `ORIGINAL_CLAUDE_CONFIG_DIR` redirection. The utility normalizes
  this before invoking `claude -p`, mirroring `mngr uncapped-claude`'s `_normalize_credentials_env()`
  (`vendor/mngr/libs/mngr_uncapped_claude/imbue/mngr_uncapped_claude/orchestrator.py:172`).
- **Ship guidance + a small importable utility.** A `libs/ai_service_helpers/` workspace package
  exposes the common operations so services don't reimplement credentialing/fallback. The skill is
  standalone; `build-web-service` gets a one-line cross-reference.
- **Extend `launch-task`, don't fork it.** Pattern 1 needs a synchronous "create → wait → collect
  result → destroy" path plus structured result extraction; today `launch-task` does launch →
  background-await → manual merge with no destroy
  (`.agents/skills/launch-task/scripts/create_worker.py` has `launch` + `await` only).

## Expected behavior

- An agent asked to "add AI to this service" loads the skill, runs the decision tree, and lands on
  exactly one of the three patterns with a clear rationale.
- **Pattern 1:** the service triggers a full agent only on explicit user feedback or an error;
  the launch is synchronous from the service's perspective (it waits), the result is applied
  (branch merged / patch applied), and the agent is destroyed. No orphaned workers or branches on
  the happy path.
- **Pattern 2:** high-volume one-shot agentic calls run headless and never degrade interactive
  chat quota or responsiveness; each call logs which billing path it used.
- **Pattern 3:** with `ANTHROPIC_API_KEY` present, calls go direct to the Anthropic API (cheapest,
  no agentic loop); when absent, they transparently fall back to headless `claude -p` with
  normalized credentials — same function signature, the caller doesn't branch.
- A service importing `ai_service_helpers` does a one-shot text/JSON completion in a few lines with
  no credentialing detail; the helper picks direct-API vs `claude -p` at runtime by key availability.
- Credentialing "just works" inside a deployed minds agent (env inherited) and fails *loudly with a
  clear message* — not silently — when no credential path is resolvable.
- The skill documents the `ANTHROPIC_API_KEY`-set-means-full-API-billing footgun (the reported
  $1,800-in-two-days case) so an agent never enables volume calls without surfacing the cost path.

## Changes

- **New skill `.agents/skills/build-ai-service/`** (symlinked into `.claude/skills/`):
  - `SKILL.md` — frontmatter (`name`, `description`) + decision tree, three-pattern playbook, the
    billing/credentialing model (post-Jun-15 two-pool table), and the auth-normalization
    explanation. Prose for nondeterministic choices; defers deterministic operations to the lib.
  - `references/billing-and-credentialing.md` — three-bucket table (API key / programmatic credit /
    interactive), the cutover-date caveat, the `CLAUDE_CONFIG_DIR` vs `ORIGINAL_CLAUDE_CONFIG_DIR`
    failure mode, and the API-key cost footgun.
  - `references/patterns.md` — worked sketch per pattern: when to pick it, the call shape, the
    lifecycle/teardown obligations.
- **New lib `libs/ai_service_helpers/`** (uv workspace member; registered in root `pyproject.toml`
  under `[tool.uv.workspace]` + `[tool.uv.sources]`):
  - `complete()` — runtime helper for patterns 2/3: direct Anthropic API if key present, else
    normalized `claude -p`; returns text or parsed JSON; logs the billing path.
  - `run_agentic_task()` — pattern 2 primitive for one-shot agentic (tool-using) headless runs.
  - `launch_full_agent()` — thin pattern-1 wrapper over the extended `launch-task` synchronous flow.
  - credential-normalization helper (env construction for `claude -p`) + a direct-API client factory
    (prompt caching per the `claude-api` skill conventions).
  - `data_types.py` (frozen `FrozenModel` config/result types), `test_ai_service_helpers_ratchets.py`
    at zero counts, README.
- **Extend `launch-task`** (`scripts/create_worker.py` + `SKILL.md`):
  - Add a synchronous "create → await → extract result → destroy" entry point composing the existing
    `launch`/`await`; add structured terminal-report extraction (parse `type`/`name` + body); add a
    `destroy` step with a keep-branch option. Cover with unit tests in `create_worker_test.py`.
- **`build-web-service`**: one-line cross-reference in its SKILL.md pointing to `build-ai-service`
  for the "this service needs to call Claude" case.
- **Tests**: unit tests for `complete()` fallback selection (key present vs absent) and credential-env
  normalization; unit tests for the new `launch-task` synchronous wrapper + result extraction. No
  string-constant tests; no tests of the decision-tree prose.

## Assumed defaults (you did not answer the Q&A — flip any of these)

- **Q1 scope → (b)** guidance + a `libs/` utility; standalone skill with a `build-web-service`
  cross-reference (not deep integration).
- **Q2 cost policy → (a)+(b)** prefer explicit `ANTHROPIC_API_KEY` for volume *and* always log the
  active billing path. No hard stop, since chat is protected regardless post-cutover.
- **Q3 `mngr uncapped-claude` vs raw `claude -p` → NEEDS YOUR CALL.** My lean: utility uses raw
  `claude -p` with a credential-normalizing env (lighter; no ephemeral mngr agent per call) and
  documents `mngr uncapped-claude` as the auto-normalizing drop-in fallback. Confirm, or pick
  "prefer uncapped-claude" if you want the mngr-blessed path primary despite per-call agent spawn cost.
- **Q4 pattern-1 lifecycle → (a)+(c)** add the synchronous create→wait→apply→destroy wrapper to
  `launch-task`, including structured result extraction and a destroy step.
- **Q5 utility surface → (b)** three design-time primitives + one runtime helper (`complete()`) that
  auto-picks direct-API vs `claude -p` for patterns 2/3.

## Open questions

- **Q3 above** — the one decision I won't finalize without you.
- Pattern 1 "apply result": for a service editing *itself*, do we merge the worker's branch into the
  service's branch automatically, or hand the diff back for review? (Determines whether the wrapper
  merges or just reports.)
- Where does `ANTHROPIC_API_KEY` come from in non-mngr deploys? mngr forwards it via
  `.mngr/settings.toml`, but a bare service host may not have it.
- Skill/lib naming (`build-ai-service` / `ai_service_helpers`) — confirm or rename.
