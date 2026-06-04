---
name: use-ai-integration
description: Use when building a service that calls Claude -- AI-driven services and AI integrations. Covers the three integration patterns (one-shot completion, one-shot agentic task, full agent), how to pick one, and the credentialing / billing / cost model. Backed by the libs/ai_integration package.
---

# Use an AI integration in a service

A service can call Claude in three ways, at escalating levels of agency. Pick the
weakest one that does the job -- it is cheaper, faster, and simpler. All three
are implemented in the `ai_integration` library so you do not hand-roll
credentialing, the `claude -p` environment fix, billing-path logging, or spend
control.

Import what you need:

```python
from ai_integration.core import run_completion, run_task, run_agent
```

The functions are `async` (services here are async FastAPI). The `claude -p`
work is offloaded to a thread internally; the direct-API path uses the async
Anthropic client.

## Decision tree -- pick the pattern

Ask, in order:

1. **Does the work need agency at all** -- i.e. tool use, reading files, multiple
   reasoning/acting steps? If **no** (classify, summarize, extract, rewrite,
   answer-from-context), use **`run_completion`** (pattern 3). This is the common
   case and the cheapest.
2. **Does it need agency, but as a single self-contained run** -- "read this
   email and file a ticket", "look at this diff and write a summary with the repo
   open"? Use **`run_task`** (pattern 2): one headless `claude -p` agentic run.
3. **Does it need a full, potentially long-running agent** -- e.g. the service
   edits itself in response to user feedback, or spins up an agent to fix an
   error? Use **`run_agent`** (pattern 1). This must be **user-triggered or
   error-triggered**, never an autonomous loop, and the launched agent must carry
   a **tightly-scoped** task.

See [references/patterns.md](references/patterns.md) for a worked sketch of each.

## Pattern 3 -- `run_completion` (no agency)

```python
result = await run_completion(
    "Classify this email's intent:\n\n" + email_body,
    service_name="email-triage",       # used to resolve the spend ceiling, if any
    model="claude-haiku-4-5",          # cheap default; override as needed
    system="You are an email triage classifier.",
)
print(result.text, result.billing_path, result.cost_usd)
```

The library routes this for you: **direct Anthropic API when `ANTHROPIC_API_KEY`
is set** (always cheaper for non-agentic work), otherwise it falls back to
headless `claude -p`. You do not choose -- routing is by key presence. Any
Anthropic API option can be passed through `anthropic_options=...` (tools,
response formats, temperature, etc.), and structured output works the same way.

**`system` is required here** (unlike `run_task`). On the keyless `claude -p`
fallback the library passes it as `--system-prompt` and disables tools
(`--tools ""`). This is not just a cost optimization: the keyless fallback is
*non-bare* (bare can't authenticate without an API key), so it always auto-loads
this repo's CLAUDE.md -- and with an empty system prompt the model answers *that*
ambient text instead of your prompt. A real `system` neutralizes it. Make it a
genuine instruction for the task ("You are an email triage classifier."), not a
placeholder.

### The onramp is automatic -- do not make the user set up a key first

A user with no API key can build and test the whole flow on the `claude -p`
fallback immediately. When they later set `ANTHROPIC_API_KEY`, every
`run_completion` call transparently upgrades to the cheaper direct API -- no code
change. While running keyless, the library **logs the calculated savings a key
would unlock** (it prices the actual call against the direct-API counterfactual).
So: do not push the user to set up a key up front; let the implicit onramp do its
job and surface the savings figure once volume makes it worthwhile.

## Pattern 2 -- `run_task` (one-shot agentic)

```python
result = await run_task(
    "Read runtime/email-triage/latest.json and draft a reply; "
    "use the repo's templates in templates/.",
    service_name="email-triage",
    spend_tracker=tracker,
)
```

Always headless `claude -p` (it has tools and file access, which a plain API call
does not). Direct API is not an option here. Tools stay enabled -- the point is to
ride the default agent. Pass `append_system="..."` to layer task instructions on
top of the default agent prompt (`--append-system-prompt`), or `system="..."` to
replace it outright (rare; you usually want the default agent here).

## Pattern 1 -- `run_agent` (full agent)

```python
from ai_integration.data_types import AgentOutcome

result = await run_agent(
    name="email-triage-selfedit-42",
    template="worker",
    runtime_dir=Path("runtime/email-triage/selfedit-42"),
    task_file=Path("runtime/email-triage/selfedit-42/task.md"),
    service_name="email-triage",
)
if result.outcome is AgentOutcome.DONE:
    ...  # the worker's branch is result.branch
```

This wraps the `launch-task` synchronous `create_worker.py run` path
(launch -> await the finish report -> structured result -> destroy). You write
the task file first (with `lead_agent` / `finish_report_path` frontmatter; see the
`launch-task` skill). **Triggering**: only on explicit user feedback or an error
-- never an autonomous loop. **Scope**: give the agent a tightly-scoped task; a
broad task in an unattended launch is how cost and time run away. What to *do*
with the returned branch (merge, hand back for review) is out of scope here and
belongs to a dedicated future skill.

## Cost control

You never have to worry about blocking the user's interactive chat: `claude -p`
and the direct API draw separate pools from interactive usage (see
[references/billing-and-credentialing.md](references/billing-and-credentialing.md)).
The live concern is **cost**, so:

- **Measure on a small sample before scaling.** Before wiring up a high-volume
  flow, run the pattern on a handful of items, look at `result.cost_usd`, and tell
  the user the projected cost ("this batch of N will cost ~$X"). For `run_task`,
  remember `claude -p` cost is dominated by per-call overhead (each invocation
  reloads the agent), so **batch rather than parallelize** (fewer, larger calls).
- **Know why `claude -p` costs more than the direct API.** It's the default
  agent's per-call context: system prompt + tool definitions + auto-loaded
  CLAUDE.md/skills, plus a multi-turn tool loop. `run_completion`'s keyless
  fallback already sheds most of this (`--system-prompt` + `--tools ""`), but the
  direct API carries none of it -- which is why a key is cheaper and why the
  library nudges you toward one once volume justifies it. (The deeper measured
  breakdown and the `--bare`-vs-keyless-auth constraint are in
  [references/billing-and-credentialing.md](references/billing-and-credentialing.md).)
- **Confirm the billing path with the user at setup.** When wiring up a service
  that will do volume, surface which path it will use and roughly what it will
  cost, and get their OK before turning it on.
- **Offer the user a spend ceiling (optional, set in `services.toml`).** There is
  no tracker to construct or pass -- spend tracking is resolved automatically from
  the service's config and keyed by `service_name`, so spend aggregates across
  *every* call for that service (persisted under `runtime/<service>/`). To enable
  it, add an `[services.<name>.ai_spend]` table:

  ```toml
  [services.email-triage.ai_spend]
  ceiling_usd = 5.0          # rolling-window budget
  window_seconds = 86400     # optional; default 24h
  ```

  With this set, each call checks the ceiling first and records its cost after;
  once the window's spend reaches the ceiling, the next call raises
  `SpendCeilingExceededError` (and logs) instead of spending silently -- the
  service can catch that to notify the user (e.g. via `send-user-message`). With
  no `ai_spend` table the calls run unbounded. **This is opt-in: tell the user a
  spend ceiling is available and let them decide whether to set one** (it does not
  require the service to be a running background process -- a spend-tracking-only
  `[services.<name>.ai_spend]` table with no `command` works too).

## What the library guarantees (so you don't have to)

- **Credentialing.** Prefers `ANTHROPIC_API_KEY`, else the inherited
  `CLAUDE_CONFIG_DIR`; raises `CredentialsUnavailableError` *loudly* if neither
  resolves, rather than failing opaquely inside `claude`.
- **The mngr `claude -p` bug.** Every spawned `claude -p` runs with
  `MAIN_CLAUDE_SESSION_ID` unset, so it does not trip mngr's session-guarded
  stop/readiness hooks. This is the main reason to call `claude -p` *through* the
  library rather than shelling out yourself.
- **Billing-path logging** on every call, and the keyless savings nudge.

Details and the footgun (a stray `ANTHROPIC_API_KEY` silently switches `claude -p`
to full-API billing) are in
[references/billing-and-credentialing.md](references/billing-and-credentialing.md).
