# Billing and credentialing model

Why the patterns are credentialed the way they are, and why a service can call
Claude heavily without ever blocking the user's interactive chat.

## Three billing buckets

| How the call is made | Bucket it draws | Blocks interactive chat? |
|---|---|---|
| Direct Anthropic API (`ANTHROPIC_API_KEY` set) | Pay-per-token API account (separate contract) | No |
| `claude -p` on a subscription, no key | Programmatic / Agent-SDK credit pool (finite, then full API rates) | No |
| Interactive Claude Code / chat / Cowork | Interactive subscription pool | -- (this is the pool to protect) |

As of the **2026-06-15 subscription split**, `claude -p` / Agent-SDK usage draws
a *separate* pool from interactive usage. So neither the direct API nor `claude -p`
competes with the user's chat quota.

Consequence: **the live concern is cost, not chat availability.** That is why the
library logs the billing path and supports a spend ceiling, rather than gating
calls to protect the chat.

## The spend ceiling (optional, `services.toml`-driven)

Spend tracking is **opt-in and configured in `services.toml`**, not in code -- the
`run_*` functions take no tracker object. The library resolves the ceiling from
`[services.<service_name>.ai_spend]` (the `service_name` every call already
passes):

```toml
[services.email-triage.ai_spend]
ceiling_usd = 5.0          # rolling-window budget
window_seconds = 86400     # optional; default 24h
```

When present, each `run_completion` / `run_task` call checks the ceiling before
spending and records the cost after; spend is **aggregated per service across
every call** via the persisted ledger at `runtime/<service_name>/ai_spend.json`
(so it survives restarts and spans all usages, not just one process). Once the
window's spend reaches the ceiling, the next call raises
`SpendCeilingExceededError` and logs, instead of spending silently; a service can
catch that to route a notice through `send-user-message`. With no `ai_spend`
table, calls run unbounded.

The `ai_spend` table is independent of `command` / `restart`: a service that
needs a budget but isn't a continuously-running background process (e.g. one
invoked on demand) can declare `[services.<name>.ai_spend]` with no `command` --
the bootstrap manager skips command-less entries, so nothing is launched, while
the spend loader still finds the budget by name.

## Why `claude -p` costs more, and the three cost levers

`claude -p` is pricier than a direct API call not because of the model but because
of the **default agent context it reloads per call**: the Claude Code system
prompt, all tool definitions, and the auto-discovered CLAUDE.md / skills -- plus it
runs a multi-turn tool loop. Three levers control this (on this repo, Haiku, a
one-line prompt):

| Config | Turns | Context | Cost | Notes |
|---|---|---|---|---|
| Default `claude -p` | ~7 | ~238k | ~$0.086 | May wander off-task (e.g. try to `git commit` an unrelated file) |
| `--system-prompt <s>` + `--tools ""` | 1 | ~13k | ~$0.016 | The flags `run_completion` uses; the ~13k is CLAUDE.md + skills |
| above **+ isolated cwd** | 1 | ~0.2k | ~$0.012 | What `run_completion`'s keyless fallback does; CLAUDE.md not loaded |
| `--bare` (+ replace) | -- | -- | -- | Strips CLAUDE.md/skills too, but **fails to auth keyless** |

Takeaways:

- **`--tools ""` is a correctness fix, not just cost.** The default agent given a
  "just answer this" prompt will use tools and act -- it may try to do unrelated
  work like committing files. The non-agentic `run_completion` fallback always
  disables tools.
- **The residual ~13k is CLAUDE.md + skills; `run_completion` sheds it via an
  isolated cwd.** `claude -p` auto-discovers CLAUDE.md / `.claude` hooks from the
  *working directory*, so the keyless completion path runs the CLI from a throwaway
  temp dir -- no project context is loaded, dropping that residual to ~0. This needs
  no key and no `--bare` (which can't authenticate keyless anyway: it requires
  `ANTHROPIC_API_KEY` or an `apiKeyHelper`, returning "Not logged in" with no key).
  `run_task` does *not* isolate cwd -- it needs the repo context for file access.
  **The library never uses bare.**
- **A required `system` on `run_completion` is load-bearing.** With a non-bare
  fallback, an empty or absent system prompt lets ambient CLAUDE.md text hijack the
  answer -- the model responds to the repo's guidance instead of the prompt. The
  isolated cwd removes that source structurally; `system` is required regardless
  because it frames the task and is the system block on the direct-API path.
- **The savings nudge is honest.** `result.cost_usd` on the fallback already
  reflects the stripped config, so the "set a key to save ~$Z" figure compares the
  *stripped* `claude -p` cost against the direct-API counterfactual, not the
  heavier default agent.

The relevant flags also include `--system-prompt-file` / `--append-system-prompt-file`
(file variants), `--json-schema` (structured output on the CLI path), and
`--max-budget-usd` (a per-invocation hard cap, complementary to the cross-call
`SpendTracker` ceiling).

## The footgun

If `ANTHROPIC_API_KEY` is set in the environment, `claude -p` bills **full API
rates** against the API account, not the subscription's programmatic credit. In a
deployed mngr agent the key is typically forwarded (via `.mngr/settings.toml`), so
`run_completion` will usually take the direct-API path -- which is what you want
(cheapest for non-agentic work), but it *is* real per-token spend. An unattended
`claude -p` loop on an API key can run up four-figure spend in a couple of days,
so surface the projected cost to the user before scaling a flow up.

## Credential resolution (what the library checks)

`run_completion` routes by key presence; all paths require *some* credential:

1. `ANTHROPIC_API_KEY` in the environment -> direct API.
2. Otherwise `claude -p`, which authenticates from the inherited
   `CLAUDE_CONFIG_DIR` (or `~/.claude`) -- `.credentials.json` (OAuth) or
   `~/.claude.json`'s `primaryApiKey`.
3. If neither resolves, the library raises `CredentialsUnavailableError` with a
   clear message rather than letting `claude` fail opaquely.

A service started from `services.toml` inherits the agent's environment (the
bootstrap manager's tmux default-command sources the host + agent env files), so
in a deployed agent both `CLAUDE_CONFIG_DIR` and (usually) `ANTHROPIC_API_KEY` are
present and `claude -p` "just works".

## The mngr `claude -p` session-hook bug

mngr sets `MAIN_CLAUDE_SESSION_ID` in an agent's environment to mark its managed
main session. Every mngr stop/readiness hook is guarded on that variable
(`[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0`). If a child `claude -p` inherits
the variable, it looks like the managed main session and engages mngr's hook
machinery -- the failure mode you hit when calling `claude -p` directly.

The library builds the `claude -p` child environment with `MAIN_CLAUDE_SESSION_ID`
**unset**, which neutralizes all those hooks (the other `MNGR_*` vars are not
load-bearing for this bug, though `build_claude_cli_env` can strip them too as
defense-in-depth). This is why services should always go
through the library rather than spawning `claude -p` themselves.
