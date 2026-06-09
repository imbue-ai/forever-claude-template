# Plan: `use-ai-integration` skill + `libs/ai_integration`

A full skill teaching an agent how to wire Claude into a service correctly, backed by a thin
workspace lib that centralizes credentialing, the mngr `claude -p` bug workaround, billing-path
selection, logging, and spend control.

> **Refined request**
>
> Create a full skill **`use-ai-integration`** + a workspace lib **`libs/ai_integration`** for AI
> integrations in services. Standalone skill; `build-web-service` gets a one-line cross-reference.
>
> **Decision tree over three escalating-agency patterns:**
> * **Pattern 3 — no agency** → `run_completion()`: direct Anthropic API if `ANTHROPIC_API_KEY`
>   present (cheap-model default e.g. Haiku, caller-overridable, prompt caching on, any Anthropic
>   API option passable; text by default with optional JSON/schema-validated structured output),
>   else fall back to headless `claude -p`. `system` is a **required** parameter: on the API path it
>   is the cache-controlled system block; on the keyless `claude -p` fallback it is passed as
>   `--system-prompt` alongside `--tools ""` (the lean non-bare config). Required because the non-bare
>   fallback always auto-loads CLAUDE.md, and an empty/absent system prompt lets that ambient text
>   hijack the response (the model would answer the repo's instructions instead of the prompt);
>   a mandatory `system` neutralizes it by construction.
> * **Pattern 2 — one-shot agentic** (tools/file access) → `run_task()`: always headless `claude -p`,
>   tools left enabled (the point is to ride the default agent). Optional `append_system`
>   (`--append-system-prompt`) layers task instructions on the default; optional `system`
>   (`--system-prompt`) fully replaces it. `--bare` is not used (it strips the agent and can't auth
>   keyless).
> * **Pattern 1 — full agent** → `run_agent()`: thin wrapper over an extended `launch-task`
>   synchronous launch -> await -> collect-structured-result -> destroy. User- or error-triggered
>   only; the self-editing-service "apply the result" flow is a *separate future skill*, out of scope.
> * All wrappers are thin; async-first surface; no built-in concurrency cap.
>
> **Credentialing / env:** prefer `ANTHROPIC_API_KEY`, else inherited `CLAUDE_CONFIG_DIR`, else
> **fail loudly**. The `claude -p` child env MUST unset `MAIN_CLAUDE_SESSION_ID` (every mngr
> hook is session-guarded, so unsetting it suffices); optionally also drop
> `MNGR_AGENT_STATE_DIR`/`MNGR_AGENT_NAME`/`MNGR_HOST_DIR`. Centralizing this is *why* services call
> `claude -p` through the lib.
>
> **Cost (chat is protected post Jun-15 split; cost is the live concern):** always log the active
> billing path; the authoring agent confirms the billing path with the user at setup. The spend
> ceiling is **optional and configured in `services.toml`** (`[services.<name>.ai_spend]` with
> `ceiling_usd` + optional `window_seconds`), not a tracker object threaded through calls: the library
> resolves it by `service_name`, estimates cost ($) from token usage, persists the ledger under
> `runtime/<service>/` (a rolling window, so spend aggregates across every call/restart), and on breach
> raises `SpendCeilingExceededError` (which the service can catch to notify via `send-user-message`)
> instead of spending silently. The `ai_spend` table is independent of `command`, so a non-running /
> on-demand service can have a ceiling too. The skill instructs the agent to *inform the user a ceiling
> is available* rather than requiring one. Document the `ANTHROPIC_API_KEY`-means-full-API-billing footgun.
>
> **`launch-task` changes (in scope):** add the synchronous launch -> await -> collect-structured-
> result -> destroy path + structured terminal-report extraction + a destroy step; reword
> `create_worker.py`'s `await` docstring + subparser help (and `lead-proxy.md` framing) so `await`
> reads as a generic poll-until-`finish_report_path` primitive with the gate cycle as one caller
> pattern.

## Overview

- One skill + one thin lib so an agent adding AI to a service picks the right pattern and never
  hand-rolls credentialing, the mngr `claude -p` bug workaround, billing selection, or spend control.
- Three escalating-agency entry points: `run_completion()` (no agency, API-or-`claude -p`),
  `run_task()` (one-shot agentic via `claude -p`), `run_agent()` (full agent via `launch-task`).
- Billing is the organizing principle: post the Jun-15 2026 split, `claude -p` / Agent-SDK usage
  draws a *separate* pool from interactive chat, so these calls can't block core chat; the live
  risk is cost, which the lib makes explicit (logging), confirmed (at authoring time), and bounded
  (runtime spend ceiling that escalates).
- Resolves the long-standing "`claude -p` sometimes fails to authenticate" and the mngr bug where an
  inherited `MAIN_CLAUDE_SESSION_ID` makes a child `claude -p` look like the managed main session.
- `launch-task` is extended (not forked) with a synchronous create -> wait -> collect -> destroy
  path and clearer, gate-agnostic `await` docs, since this PR adds a new gate-less usage.
- The zero-setup onramp is **implicit in the library**, not a process the agent narrates:
  `run_completion()` uses the direct Anthropic API whenever a key is present (always cheaper than
  `claude -p` for non-agentic work) and silently falls back to `claude -p` when there's no key. So a
  keyless user develops and tests immediately, and adding `ANTHROPIC_API_KEY` later transparently
  upgrades every call to the cheaper path — no code change, no agent decision. Because the library
  runs the `claude -p` calls, the keyless nudge is a **calculated** figure, not a rule of thumb: it
  captures each call's actual token usage/cost and computes the counterfactual direct-API cost for the
  same usage, so it can report concrete cumulative savings ("you've spent ~$X on `claude -p`; the same
  calls via the direct API would have cost ~$Y — set `ANTHROPIC_API_KEY` to save ~$Z"). The library
  emits the nudge and guarantees the routing so correctness never depends on the agent remembering to
  do it; the skill instructions still explain this behavior so the agent understands it and can
  surface it to the user.
- The skill steers agents to **measure cost on a small sample before building a high-volume flow**,
  and to **surface the cost/approach tradeoff to the user** with real numbers — prose guidance, not a
  template. Grounded in the `claude -p` cost profile, which has **three controllable levers** (on this
  repo, Haiku, a trivial prompt):
  - *Default agent* `claude -p` reloads the full Claude Code context (system prompt + tool
    definitions + auto-discovered CLAUDE.md/skills) **and** runs a multi-turn tool loop —
    ~7 turns / ~$0.086 for a one-line prompt, and it may wander off-task (e.g. trying to commit an
    unrelated file). This is the expensive, *dangerous* default for non-agentic work.
  - *Stripped non-bare* (`--system-prompt <s>` + `--tools ""`) drops it to **1 turn / ~$0.016 / ~13k
    context** and keeps it on task. This is what `run_completion`'s keyless fallback uses. The
    residual ~13k is CLAUDE.md + skills, which only `--bare` removes.
  - *`--bare`* strips CLAUDE.md/skills too, but **cannot authenticate without an API key** (OAuth and
    keychain are never read — bare returns "Not logged in"). So bare is
    unavailable on the keyless subscription path, and once a key exists `run_completion` routes to the
    direct API anyway — so the library never uses bare.
  Two consequences the guidance names explicitly: (a) for agentic `run_task` work, batch rather than
  parallelize (fewer, larger calls amortize the per-call agent reload); and (b) when no agency is
  needed, the direct Anthropic API carries none of this overhead and is the cheapest path — the gap to
  the stripped keyless fallback narrows from ~10x (vs the default agent) toward ~2–3x for small
  prompts, but direct API still wins.

## Expected behavior

- An agent asked to "add AI to this service" loads the skill, runs the decision tree, and lands on
  exactly one pattern with a clear rationale, then calls the matching `run_*` helper.
- **`run_completion()`** returns text (or schema-validated structured output) for non-agentic work;
  uses the direct Anthropic API when a key is present, otherwise transparently falls back to
  `claude -p` — same signature, caller doesn't branch; any Anthropic API option is passable.
- **`run_task()`** runs a one-shot agentic `claude -p` (tools/file access) with a correctly
  normalized child env.
- **`run_agent()`** launches a tightly-scoped full agent, waits for its finish report, returns a
  structured result, and destroys the agent; applying that result (e.g. self-edit merge) is left to
  a future skill.
- Every paid call logs which billing path it used; the active path is confirmed with the user at
  service-authoring time. When a service opts into a ceiling via `services.toml`, cumulative estimated
  spend is tracked per-service (aggregated across every call) and, on exceeding the configured ceiling,
  further paid calls stop (raise) rather than being silently billed; with no ceiling configured, calls
  run unbounded.
- Credentialing "just works" inside a deployed minds agent (env inherited) and fails *loudly with a
  clear message* when no credential path resolves.
- A child `claude -p` never engages mngr's stop/readiness-hook machinery (its `MAIN_CLAUDE_SESSION_ID`
  is unset), fixing the mngr bug.
- A service awaiting a launched agent reads `await` as a plain poll-until-report primitive — no gate
  semantics implied.
- A keyless user can develop and test an AI flow end-to-end on the implicit `claude -p` fallback;
  adding a key later transparently upgrades non-agentic calls to the cheaper direct API with no code
  change. The direct-API-vs-`claude -p` routing is never a manual choice — the library decides by key
  presence.
- Before committing to a high-volume flow, the agent runs a small metered sample (per-item cost and
  latency) and surfaces the cost magnitude to the user — e.g. "this batch of N will cost ~$X" or, for
  agentic `run_task()` work where direct API is not an option, whether to batch vs parallelize. This
  is about whether the volume is worth it, not about picking the billing path (the library already
  routes that).

## Changes

- **New skill `.agents/skills/use-ai-integration/`** (symlinked into `.claude/skills/`): SKILL.md
  (decision tree, three-pattern playbook, billing/credentialing model with the post-Jun-15 two-pool
  table, the `MAIN_CLAUDE_SESSION_ID` rationale, tight-scoping guidance for launched agents, and the
  **measure-cost-on-a-sample-then-surface-the-magnitude** practice — including the `claude -p`
  per-call overhead profile and "batch over parallelize" for agentic `run_task()` work. The
  instructions explain the implicit onramp/routing and the calculated-savings nudge so the agent
  understands the library's behavior and can communicate it; the library enforces that routing
  regardless, so the agent isn't the single point of failure. The cost-probe guidance is about
  whether the volume is worth it, not about manually picking the billing path) + `references/` for the
  billing/credentialing reference (carrying the cost numbers) and worked per-pattern
  sketches.
- **New lib `libs/ai_integration/`** (uv workspace member, registered in root `pyproject.toml`):
  the three async `run_*` functions plus shared internals — credential resolution (+ loud failure),
  implicit billing-path routing for `run_completion()` (direct API when a key is present, else
  `claude -p` fallback; adding a key later upgrades transparently). The keyless path captures each
  `claude -p` call's reported usage/cost (e.g. via `--output-format json`) and, using a per-model
  direct-API price table (the same table the spend tracker uses to estimate cost), computes the
  counterfactual cost to report concrete cumulative savings ("set a key to save ~$Z"); confirm the
  exact `claude -p` cost/usage field names at implementation.
  Also: `claude -p` child-env construction (unset `MAIN_CLAUDE_SESSION_ID`;
  optional `MNGR_*` strip), direct-Anthropic-API client factory (cheap-model default, prompt caching,
  structured-output support), billing-path logging, and the per-service spend ceiling resolved from
  `services.toml` (`load_spend_tracker(service_name)`; optional, off when unconfigured; raises on
  breach). Frozen data types, README, zero-count ratchet test.
- **Extend `launch-task`**: add a synchronous launch -> await -> collect-structured-result ->
  destroy path + structured terminal-report extraction + a destroy step in `create_worker.py`; and
  reword its `await` docstring + subparser help and the `lead-proxy.md` framing so `await` reads as a
  generic poll-until-`finish_report_path` primitive with the gate cycle as one caller pattern.
- **`build-web-service`**: one-line cross-reference pointing to `use-ai-integration` for "this
  service needs to call Claude."
- **Tests**: unit coverage for `run_completion()` fallback selection (key present vs absent),
  `claude -p` child-env construction (asserting `MAIN_CLAUDE_SESSION_ID` is unset), spend-ceiling
  enforcement and `services.toml` resolution (`load_spend_tracker`: present / absent / command-less /
  missing-ceiling), and the new `launch-task` synchronous wrapper + result extraction. No
  string-constant tests; no tests of the decision-tree prose.
- **Changelog**: per-project entries for the projects this PR touches (the `dev/` synthetic project
  for the new skill/lib + root registration, and `libs/mngr` if the launch-task script lives there —
  confirm path at implementation).
