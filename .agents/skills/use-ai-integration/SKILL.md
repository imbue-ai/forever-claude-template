---
name: use-ai-integration
description: Use when code needs to call Claude -- an AI-driven service, an AI integration, or a skill's scripted model step. Covers the three scenarios (one-shot completion, one-shot agentic task, full agent), the fact that a scripted step's model lives in the code (not the chat's `/model`), doing web search through an agent, forecasting cost/time before scaling, and the cost / credentialing model.
---

# Calling Claude from code

This is the shared reference for the mechanics of calling Claude from code:
which path to use, the call surface, and the cost model. Whatever sent you here
-- building an AI-driven service, scripting a skill's `[ai-script]` step, or
adding an AI integration elsewhere -- supplies the framing; this skill is the
how.

Code reaches Claude in one of two ways, depending on whether `ANTHROPIC_API_KEY`
is set in the environment: with a key, call `litellm` directly; without one, use
the `claude -p` helper in `scripts/claude_p.py`.

Which path applies is fixed for a deployment -- it does not change at runtime, so
**do not handle both.** Check once, up front, with a shell command, and
implement only the path that applies:

```bash
[ -n "$ANTHROPIC_API_KEY" ] && echo keyed || echo keyless
```

If keyed, write only the litellm path; if keyless, write only the `claude -p`
path. Branching on the key at call time is dead weight. If the user decides to
add an API key, you can do a simple migration.

## The model lives in the code, not in the chat

A scripted step's model is set **in the code** -- a constant in the script (or a
`--model` flag), e.g. `WRITE_MODEL = "claude-haiku-4-5"`. It is completely
separate from the model the agent uses to talk to the user in the chat. The
chat's `/model` and `/fast` commands change **only the conversation**; they have
zero effect on any pipeline, service, or scripted step.

So when a user says a **generated pipeline or artifact** is slow or expensive:

- The lever is that model constant. Change it in the code (or, if a background
  worker owns the artifact, send the worker a message to change it). A high-volume
  per-item step should default to the cheapest model that does the job (e.g.
  Haiku); reserve a bigger tier for low-volume judgement steps.
- **Never tell the user to run `/model` or `/fast`.** Those change the chat, not
  the artifact, and pointing the user at them to "make the pipeline faster" is
  simply wrong and confusing.

Convention: expose the model as a single top-of-file constant plus a `--model`
override, so switching it is a one-line change (and a worker can be told exactly
what to flip).

## Pick the scenario (weakest that does the job)

The call falls into one of three scenarios, by how much agency Claude
needs. Pick the weakest -- it is cheaper, faster, and simpler.

1. **One-shot completion** -- no agency: classify, summarize, extract, rewrite,
   answer-from-context. One prompt, one response, no tools. The common case.
2. **One-shot agentic task** -- a single self-contained job that needs tools or
   file access ("read this file and act", "summarize the diff with the repo
   open"). **This is also how you do web search from code** -- see below.
3. **Full agent** -- a full, possibly long-running agent that runs in its **own
   git worktree** (a `launch-task` worker). Reach for this over scenario 2 when
   Claude edits code that must be tested and validated, or when several agents
   work in the same repo and their changes must not collide. **User- or
   error-triggered only, never an autonomous loop**, with a tightly-scoped task.

## Scenario 1 -- one-shot completion

For a plain completion with **no tools**. If the step needs to search the web,
that is not this scenario -- do not reach for a server-side search tool here; use
an agent (scenario 2, and see "Web search" below).

**Keyed (`ANTHROPIC_API_KEY` set): call litellm directly.** It is cheaper than
`claude -p` for non-agentic work, and it gives you structured output, tools,
temperature, etc. with no wrapper of ours in the way. `litellm` is in the root
`pyproject.toml`; read its docs for the call surface. Sketch:

```python
from litellm import completion, completion_cost

resp = completion(
    model="claude-haiku-4-5",
    messages=[
        {"role": "system", "content": "You are an email triage classifier."},
        {"role": "user", "content": email_body},
    ],
)
text = resp.choices[0].message.content
cost = completion_cost(completion_response=resp)  # USD for this call
```

**Keyless (no key): copy `scripts/claude_p.py` and call `claude_p_completion`.**
It disables tools and runs from an isolated working directory so the repo's
`CLAUDE.md` / `.claude` hooks can't hijack the answer; `system` is required.

```python
from claude_p import claude_p_completion  # the file you copied in

result = claude_p_completion(
    "Classify this email's intent:\n\n" + email_body,
    system="You are an email triage classifier.",   # required
    model="claude-haiku-4-5",
)
print(result.text, result.cost_usd, result.usage)
```

Both `completion` and `claude_p_completion` are synchronous (no asyncio). Once
you have confirmed the prompt + model combination works and produces good
results on a few items, run a batch concurrently with a thread pool
(`concurrent.futures.ThreadPoolExecutor`) rather than one at a time -- the
throughput difference is large. When you productionize a fanned-out or
tool-using call, read
[references/hardening-scripted-calls.md](references/hardening-scripted-calls.md)
(the worker's reference) for concurrency, retries, and the keyed-path pitfalls.

## Scenario 2 -- one-shot agentic task

Always `claude -p` (it has tools and file access; a plain API call does not), so
this path is the same whether or not a key is set. Copy `scripts/claude_p.py` and
call `claude_p_task`: tools stay enabled, it runs in the repo working directory,
and it defaults `permission_mode="bypassPermissions"` (load-bearing -- a headless
run has no human to approve tool use).

```python
from claude_p import claude_p_task

result = claude_p_task(
    "Read runtime/email-triage/latest.json and draft a reply using templates/.",
    append_system="Only touch files under runtime/email-triage/.",
)
```

`append_system` layers instructions on the default agent; pass `system` to
replace it outright. The default agent prompt is many tokens, but it is useful
instruction for agentic work, so overwrite it only when you have a good reason.
Cost is dominated by per-call overhead, so **batch** items into fewer, larger
calls rather than one call per item.

## Scenario 3 -- full agent

Reach for this over scenario 2 when the work needs its **own git worktree**:
Claude is editing code that has to be tested and validated, or other agents are
working in the same repo and the changes must not collide. A `launch-task` worker
gives the run an isolated branch and worktree; scenario 2 instead runs in the
caller's own working directory.

Launch the worker synchronously and collect its structured result -- do not wrap
it; call the script directly:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch-sync \
  --name email-triage-fix-123 --template worker \
  --runtime-dir runtime/email-triage/fix-123 \
  --task-file  runtime/email-triage/fix-123/task.md \
  --timeout 30m --result-json runtime/email-triage/fix-123/result.json
```

It launches, waits for the worker's finish report in the foreground, writes a JSON
result (`timed_out`, `type`, `name`, `body`, `branch`, `raw_report`) to
`--result-json`, and destroys the worker (the `mngr/<name>` branch survives).
Write the task file first with `lead_agent` / `finish_report_path` frontmatter
(see the `launch-task` skill). **User- or error-triggered, tightly scoped** -- a
broad unattended launch is how cost and time run away. What to do with the
returned branch (merge, review) is your concern.

## Web search -- use an agent, not a server-side tool

When a scripted step needs to research the web (fetch recent context, look up
facts), do it with an **agent** (scenario 2), not by attaching a server-side
web-search tool to a completion.

Why: a server-side search tool (e.g. Anthropic's `web_search_20250305`) runs the
search **on the model provider's own infrastructure**, so it only exists when the
request lands on that provider -- it welds the step to that vendor and cannot
follow you to another model. It also drags a completion onto a fragile tool code
path (see the worker reference for the litellm breakage this caused). An agentic
task sidesteps both: `claude -p` has a built-in `WebSearch` tool, the search just
happens inside the run, and on a keyless deployment it needs no extra credential.
`claude -p` can itself be pointed at another base URL / model, so this is not
locked to one vendor.

- **Batch, don't fan out per item.** An agent reloads a large context per call
  (see `references/billing-and-credentialing.md`), so one agent per item can cost
  *more* than it saves. Batch several items into one agent call, and measure it
  (next section) before scaling.
- **Escape hatch -- seriously large-scale scraping.** If the volume is high
  enough that `claude -p` can't keep up (or is too costly per call), that is the
  point to suggest the user set up their own **dedicated search API** connection
  (e.g. Tavily, Brave, Serper, Exa): your code calls it directly and feeds the
  results into the prompt as context. That is model-agnostic and scales, at the
  cost of a new credential to hold -- so raise it only when the scale actually
  calls for it, not by default.

## Forecast cost and time before you scale

The failure to avoid: a user surprised by how slow or expensive the finished
thing is. Forecast it **before** the spend, not after.

Cost/time drivers for a scripted AI step:

- **Fan-out** -- N items times a per-item model call is the usual dominant cost.
- **Tools / web search** -- billed separately from tokens, and each tool turn
  re-feeds its results back as input tokens, so a searching call costs well more
  than a plain completion.
- **Retries** -- a validate-and-retry loop multiplies a call's cost.

Two touches, so nothing lands as a surprise:

1. **Lead, in the plan (before building):** an order-of-magnitude heads-up --
   "this makes ~15 searching model calls per run, likely a few dollars and a few
   minutes; I'll measure it exactly at the first checkpoint." Do this even when
   you produced the sample **by hand in-context** -- the automated pipeline will
   fan the same work into N metered calls, and the free-feeling sample hides that.
2. **Worker, at Gate 1 (a required outline field):** a **measured** extrapolation
   -- run one real unit, capture its actual cost + wall-clock, extrapolate to the
   full run. The how is in
   [references/hardening-scripted-calls.md](references/hardening-scripted-calls.md);
   the requirement lives in the skill outline
   (`.agents/shared/worker/references/skill-outline-fields.md`).

**Measure on a small sample before scaling.** Run the scenario on a handful of
items, check the cost, and tell the user the projected cost before turning on a
volume flow.

## Cost and the keyed onramp

A keyless caller can tell the user what each call costs and what a key would save,
so they can decide when volume justifies setting `ANTHROPIC_API_KEY`:

- `claude_p_completion` / `claude_p_task` return the **actual** `cost_usd` that
  `claude -p` reported, plus the token `usage`.
- Reprice that usage at the keyed model's rate with litellm to estimate the
  savings -- no price table to maintain, litellm carries the prices:

  ```python
  from litellm import cost_per_token

  prompt_cost, completion_cost = cost_per_token(
      model="claude-haiku-4-5",
      prompt_tokens=result.usage.input_tokens,
      completion_tokens=result.usage.output_tokens,
  )
  keyed_estimate = prompt_cost + completion_cost
  savings = result.cost_usd - keyed_estimate   # surface this to suggest a key
  ```

## References

- [references/hardening-scripted-calls.md](references/hardening-scripted-calls.md)
  -- **worker reference.** Read it when you are the background harden/crystallize
  worker turning a scripted call into production code: measuring cost, concurrency,
  retries, tool-use caps, and the keyed-path (litellm vs `anthropic` SDK)
  pitfalls. A lead building a sample does not need it.
- [references/billing-and-credentialing.md](references/billing-and-credentialing.md)
  -- the billing buckets, why `claude -p` costs more than the direct API, the
  credentialing model, and the footgun (a stray `ANTHROPIC_API_KEY` switches
  `claude -p` to full-API billing).
