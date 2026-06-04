# ai_integration

Helpers for calling Claude from a service. Three escalating-agency entry points,
with credentialing, the `claude -p` env normalization (the `MAIN_CLAUDE_SESSION_ID`
bug fix), billing-path logging, and per-service spend control handled for you.

- `run_completion(prompt, *, system, ...)` -- no agency: direct Anthropic API when
  `ANTHROPIC_API_KEY` is set (always cheaper for non-agentic work), else `claude -p`.
  Routing is implicit by key presence; the keyless path logs the calculated savings
  a key would unlock. `system` is **required** -- on the keyless `claude -p`
  fallback it is passed as `--system-prompt` (with `--tools ""`) so the call stays
  lean and isn't hijacked by the auto-loaded CLAUDE.md.
- `run_task(...)` -- one-shot agentic task (tools / file access) via `claude -p`;
  tools stay enabled, with optional `system` / `append_system` to shape the agent.
- `run_agent(...)` -- a full agent via the `launch-task` synchronous
  launch -> await -> collect -> destroy path.

Spend control is **opt-in via `services.toml`**, not a tracker passed in code: add
`[services.<service_name>.ai_spend]` with `ceiling_usd` (and optional
`window_seconds`) and `run_completion` / `run_task` enforce it automatically,
keyed by `service_name` and aggregated across every call. No table -> unbounded.

See the `use-ai-integration` skill for when to pick each pattern and the billing
model.
