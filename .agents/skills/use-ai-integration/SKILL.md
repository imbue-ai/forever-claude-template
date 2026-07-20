---
name: use-ai-integration
description: Use when writing or reasoning about code that calls Claude -- an AI-driven service, an AI integration, or a skill's scripted model step. Covers the three scenarios (one-shot completion, one-shot agentic task, full agent) and the cost / credentialing model.
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

A caller's model is set **in the code** -- expose it as a top-of-file constant
plus a `--model` override (e.g. `WRITE_MODEL = "claude-haiku-4-5"`), so switching
it is a one-line change. It is independent of the chat's `/model`, which changes
only the conversation.

## Pick the scenario (weakest that does the job)

The call falls into one of three scenarios, by how much agency Claude
needs. Pick the weakest -- it is cheaper, faster, and simpler.

1. **One-shot completion** -- no agency: classify, summarize, extract, rewrite,
   answer-from-context. One prompt, one response, no tools. The common case.
2. **One-shot agentic task** -- a single self-contained job that needs tools or
   file access ("read this file and act", "summarize the diff with the repo
   open"). This is also how you **search the web** -- `claude -p` has a built-in
   `WebSearch` tool.
3. **Full agent** -- a full, possibly long-running agent that runs in its **own
   git worktree** (a `launch-task` worker). Reach for this over scenario 2 when
   Claude edits code that must be tested and validated, or when several agents
   work in the same repo and their changes must not collide. **User- or
   error-triggered only, never an autonomous loop**, with a tightly-scoped task.

## Scenario 1 -- one-shot completion

For a plain completion with **no tools**. If the step needs a tool at all --
web search or otherwise -- reach for an agent (scenario 2), not a server-side
provider tool bolted onto a completion. A server-side tool runs on the provider
that hosts it, welding the step to one vendor, and drags a plain completion onto
a fragile tool code path; an agent's built-in tools have neither problem.

**Keyed (`ANTHROPIC_API_KEY` set): call litellm directly.** It is cheaper than
`claude -p` for non-agentic work, and it gives you structured output, tools,
temperature, etc. with no wrapper of ours in the way. `litellm` is in the root
`pyproject.toml`; read its docs for the call surface. Sketch:

```python
import os
from litellm import completion, completion_cost

# If the deployment routes Claude through a proxy, it advertises ANTHROPIC_BASE_URL.
# litellm reads a differently-named var and is picky about a trailing slash, so
# resolve it explicitly -- this keeps the litellm-direct path working everywhere.
api_base = (os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/") or None

resp = completion(
    model="claude-haiku-4-5",
    api_base=api_base,
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
throughput difference is large. Use enough workers to actually saturate the work
(the calls are I/O-bound, so this can be well into the dozens); back off only if
you hit provider rate limits. When you need structured output, prefer the
provider's own JSON / structured-output mode over parsing free text and retrying
-- it is what keeps the response well-formed.

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

- **Measure before scaling, don't guess.** Run **one real unit** of the work,
  read its **actual** cost and wall-clock off the response (`completion_cost(...)`
  or `result.cost_usd`, not a token estimate), and extrapolate to the full run
  (`N x per-unit`, plus any retries and per-tool/search fees, divided by your pool
  size for wall-clock). Tell the user that projected cost/time before you turn on
  a volume flow.

See [references/billing-and-credentialing.md](references/billing-and-credentialing.md)
for the billing buckets, why `claude -p` costs more than the direct API, the
credentialing model, and the footgun (a stray `ANTHROPIC_API_KEY` switches
`claude -p` to full-API billing).
