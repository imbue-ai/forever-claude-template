# ai_integration

Helpers for calling Claude from a service. Three escalating-agency entry points,
with credentialing, the `claude -p` env normalization (the `MAIN_CLAUDE_SESSION_ID`
bug fix), billing-path logging, and per-service spend control handled for you.

- `run_completion(...)` -- no agency: direct Anthropic API when `ANTHROPIC_API_KEY`
  is set (always cheaper for non-agentic work), else `claude -p`. Routing is
  implicit by key presence; the keyless path logs the calculated savings a key
  would unlock.
- `run_task(...)` -- one-shot agentic task (tools / file access) via `claude -p`.
- `run_agent(...)` -- a full agent via the `launch-task` synchronous
  launch -> await -> collect -> destroy path.

See the `use-ai-integration` skill for when to pick each pattern and the billing
model.
