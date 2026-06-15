# AI-driven services

How a service in this repo calls Claude. The goal is the smallest possible
surface: no bespoke "AI integration" library, no routing layer, no spend
tracking. A service that needs Claude either calls `litellm` directly (when an
API key is present) or shells out to `claude -p` (when it isn't), and the
`use-ai-integration` skill teaches the building agent which to do.

## Overview

- A service reaches Claude in one of two ways, chosen by the *building agent* at
  the time it writes the service -- not by a runtime router. The choice is
  driven entirely by whether `ANTHROPIC_API_KEY` is set in the service's
  environment.
- **Keyed:** the agent writes `litellm` directly (added to the root
  `pyproject.toml` as a dependency; the agent reads litellm's docs as needed).
  Nothing is wrapped or abstracted on our side.
- **Keyless:** the agent copies a small, self-contained `claude -p` -> JSON
  helper that the skill ships as a reference snippet. The snippet encodes the
  things that are easy to get wrong by hand: unsetting `MAIN_CLAUDE_SESSION_ID`
  (so the spawned `claude -p` doesn't trip mngr's session hooks); distinguishing
  the success vs. error arms of the JSON result; and, for the completion
  scenario, disabling tools and running from an isolated working directory so the
  repo's `CLAUDE.md` / `.claude` hooks can't hijack a non-agentic answer. The
  details live in Changes.
- A service's need falls into one of three scenarios, distinguished by how much
  agency Claude needs. The agent classifies which scenario it's building and
  implements accordingly; none of these is a library function:
  - **One-shot completion** -- no agency: classify, summarize, extract, rewrite,
    answer-from-context. One prompt, one response, no tools. Implemented with
    litellm (keyed) or the `claude -p` snippet (keyless).
  - **One-shot agentic task** -- a single self-contained job that needs tools or
    file access ("read this file and act"). Implemented with the `claude -p`
    snippet, tools left on.
  - **Full agent** -- a full, possibly long-running agent (the service edits
    itself on feedback, or launches an agent to fix an error). Implemented by
    launching a `launch-task` worker synchronously.
  - Pick the weakest scenario that does the job -- it is cheaper, faster, and
    simpler.
- Cost is surfaced, not tracked. `claude -p` reports the actual cost of each
  call; the skill teaches the agent to reprice that call's token usage against
  litellm's own price data to show a concrete "add a key and save ~$X" figure.
  There is no spend ceiling, no persisted ledger, and no price table we
  maintain.
- The design principle is a minimal maintained surface: the only thing we ship
  is skill guidance plus a copyable snippet. Everything else is either a
  third-party library (litellm) or the agent's own per-service code, so there is
  almost no code of ours to keep in sync with Claude's or litellm's evolution.

## Expected behavior

- A developer (or agent) building a service that needs Claude consults the
  `use-ai-integration` skill, decides which of the three scenarios applies,
  checks whether `ANTHROPIC_API_KEY` is set, and implements the appropriate
  path. There is no single entry point they import that hides this decision.

- **One-shot completion (no agency).** The common, cheapest case.
  - *Keyed:* the service calls `litellm` directly. It gets back text plus token
    usage and a per-call cost from litellm. Structured output, tools,
    temperature, model choice, etc. are whatever litellm exposes -- we add
    nothing on top.
  - *Keyless:* the service runs `claude -p` via the copied helper and gets back
    a small typed result carrying the response text, the reported `cost_usd`,
    the token `usage`, and the raw JSON. On this path the helper disables tools
    and runs from an isolated working directory so the repo's `CLAUDE.md` /
    `.claude` hooks can't hijack the answer.

- **One-shot agentic task.** A single self-contained job that needs tools or
  file access. The service runs `claude -p` via the copied helper with tools
  left on, in the repo working directory so the agent can read/write files. It
  relies on `bypassPermissions`, since a headless run has no human to approve
  tool use. The same typed result (text, `cost_usd`, `usage`, raw) comes back.
  In both keyless cases the helper unsets `MAIN_CLAUDE_SESSION_ID` and raises
  loudly on a `claude -p` error result (e.g. max-turns) or malformed JSON
  rather than silently returning empty text.

- **Full agent.** For the rare case that warrants a full, possibly long-running
  agent -- user- or error-triggered, tightly scoped, never an autonomous loop --
  the service launches a `launch-task` worker synchronously: launch, await the
  worker's finish report, collect a structured result (outcome, branch, the
  worker's report), then tear the agent down. The worker's branch survives
  teardown; what to do with it (merge, review) is the service's concern.

- **Cost / onramp.** A keyless service can report what each call actually cost
  and, using litellm's price data, what the same call would cost with a key --
  so the user can decide when volume justifies setting `ANTHROPIC_API_KEY`. No
  budget is enforced; if the user wants a ceiling they build it themselves.

- **Credentialing.** A deployed mngr agent normally has both `CLAUDE_CONFIG_DIR`
  and (usually) `ANTHROPIC_API_KEY` in its environment, so both paths "just
  work." A service with neither fails with a clear error from the path it
  attempted, not an opaque auth failure.

- **Billing isolation.** `claude -p` and the direct API draw separate pools from
  the user's interactive chat, so heavy service usage never competes with the
  chat quota -- the live concern is cost, not chat availability. The footgun: with
  `ANTHROPIC_API_KEY` set, `claude -p` bills full API rates against the API
  account, so an unattended keyed `claude -p` loop can run up real spend.

## Changes

- **Add `litellm` to the root `pyproject.toml`** as the supported library for
  the keyed completion path.
- **Create the `use-ai-integration` skill** as a purely instructional guide:
  - how to detect a key (`os.environ.get("ANTHROPIC_API_KEY")`) and branch;
  - the three scenarios and when to use each;
  - the keyed completion path via litellm, including how to read litellm's
    reported cost;
  - the keyless `claude -p` snippet and which flag set applies to a completion
    vs. an agentic task;
  - the cost/onramp nudge (reprice `usage` via litellm's price data to estimate
    the savings a key would unlock);
  - the billing/credentialing guidance and the `ANTHROPIC_API_KEY` footgun.
- **Ship the `claude -p` helper as a copyable reference snippet** under the skill
  -- a real, syntax-valid `.py` reference file the agent copies/adapts into its
  service, not an importable package. The completion and agentic-task scenarios
  differ only in CLI flags and working directory, not in the subprocess/parsing
  logic, so this is **one snippet** (not two): a shared core plus two thin
  wrappers (completion, task) that bake the per-scenario gotchas in as defaults,
  so a caller can't forget them.
  - *Shared core (both scenarios):* unset `MAIN_CLAUDE_SESSION_ID` in the child
    environment (optionally also the `MNGR_AGENT_*` identity vars); invoke
    `claude -p <prompt> --output-format json --model <m>`; run the subprocess off
    the event loop (a worker thread) so an async service isn't blocked; raise
    with the captured stderr on a non-zero exit; parse the JSON result
    distinguishing the **success arm** (`subtype == "success"`, has `result`)
    from the **error arm** (`is_error` true -- e.g. `error_max_turns` -- carrying
    `errors`), raising on the error arm or a missing `result` rather than
    returning empty text; return a small typed result carrying `text`,
    `cost_usd` (from `total_cost_usd`), `usage` (input/output plus cache-read and
    cache-write tokens), and the raw JSON.
  - *Completion wrapper:* disable tools (`--tools ""`) **and** run from an
    isolated temporary working directory so `claude -p` doesn't auto-discover the
    repo's `CLAUDE.md` / `.claude` hooks (which otherwise bleed into -- and
    intermittently hijack -- a non-agentic answer); require a real `system`
    (`--system-prompt`) as the neutralizing instruction. (`--bare` would strip
    project context too, but it can't authenticate without a key, so the isolated
    cwd is the keyless workaround.)
  - *Task wrapper:* leave tools enabled and run in the repo working directory (it
    needs file access); default `--permission-mode bypassPermissions`
    (load-bearing -- a headless run has no human to approve tool use, so otherwise
    Read/Write/Bash are auto-denied); accept `--append-system-prompt` to layer
    instructions on the default agent, or `--system-prompt` to replace it.
- **Add a synchronous launch path to the `launch-task` tooling** for the
  full-agent scenario: launch a worker, await its finish report, return a
  structured result (outcome, branch, report), and destroy the agent. The skill
  points the building agent at this rather than wrapping it in a library.

## Notes / accepted trade-offs

- **Snippet drift (accepted):** because the `claude -p` helper is copied rather
  than imported, a future change to `claude -p`'s JSON shape or the
  session-hook fix won't propagate to services that already copied it. This is
  chosen deliberately to avoid maintaining a library; if drift becomes painful,
  promoting the snippet to a one-module `libs/` helper is the obvious escape
  hatch.
