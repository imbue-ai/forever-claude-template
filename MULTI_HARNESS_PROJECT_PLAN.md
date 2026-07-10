# Multi-harness support: full project plan

Everything needed to bring codex/antigravity/opencode to real parity with
claude in fct + minds, beyond the `.mngr/settings.toml` and
`libs/bootstrap/src/bootstrap/manager.py` changes already landed (see
`changelog/multi-harness-support.md`). Organized by phase; each phase
depends on the ones before it actually working.

---

## Phase 1 — DONE

`.mngr/settings.toml` (full 4-way `agent_types`/`create_templates` chains)
and `libs/bootstrap/manager.py` (`FCT_HARNESS` dispatch). See the changelog
entry for the open issues that came out of this phase.

---

## Phase 2 — DONE

Nothing past Phase 1 worked until this landed — the Docker image previously
only installed claude and only wired claude's mngr plugin into the venv.

The easiest, most mechanical phase in this whole plan: the plugin *source*
for all three is already sitting in the repo (`vendor/mngr` is a full
vendored copy of the whole mngr monorepo -- `vendor/mngr/libs/mngr_codex`,
`mngr_antigravity`, `mngr_opencode` all already exist on disk, confirmed).
Only the wiring is missing, and the install commands are already known
(codex: `npm i -g @openai/codex@<version>`; antigravity: the curl installer
we already ran live on this machine; opencode: its own installer with a
`VERSION=<version>` env var) -- no new source to write, no design
decisions, closer to a routine dependency-bump PR than to Phase 1's work:

- **`pyproject.toml`**: add 3 more `[tool.uv.sources]` lines mirroring the
  existing `imbue-mngr-claude = { path = "vendor/mngr/libs/mngr_claude",
  editable = true }` (line 72) -- one each for `imbue-mngr-codex`,
  `imbue-mngr-antigravity`, `imbue-mngr-opencode`, pointed at their
  already-vendored paths. Add the `_usage` plugin variants too if usage
  tracking in the UI matters (Phase 5 territory).
- **`Dockerfile`**: add `CODEX_VERSION`/`OPENCODE_VERSION` build args
  (antigravity has no version-pin capability, nothing to add there --
  confirmed in Phase 1). Add 3 more `COPY vendor/mngr/libs/mngr_<x>/
  pyproject.toml ...` lines mirroring the existing claude one (line 61), and
  the actual CLI-binary install steps in `scripts/setup_system.sh`,
  mirroring however claude's install currently works there.
- **`scripts/fct_seed.sh`** and anything else path-sensitive to
  `/mngr/code/vendor/mngr/libs/mngr_claude` specifically: audit for
  claude-only assumptions once the other three are vendored alongside it.

---

## Phase 3 — Per-harness instruction/hook/config/MCP parity

Deep-researched against each harness's real docs, source, and (for
antigravity) a live authenticated install — not guessed. Organized by
category; each table row names the concrete source file(s) to treat as
canonical.

### A. Hooks (`scripts/claude_*.sh` + `.claude/settings.json`'s `hooks` block)

The part that genuinely can't be templated text-to-text — the underlying
*mechanisms* differ structurally per harness, not just the file format.

| # | Hook (source file) | Claude event | Codex | Antigravity (agy) | OpenCode |
|---|---|---|---|---|---|
| 1 | `claude_update_plugin.sh` | SessionStart | **Feasible** — real `SessionStart` event, `~/.codex/hooks.json` | **Partial** — no SessionStart event; gate on `invocationNum==1` inside `PreInvocation` | **Feasible, differently** — no hook needed at all; put install/update calls directly in the plugin's top-level async function body (runs once at `opencode serve` boot) |
| 2 | `claude_prevent_commit_rewrite.sh` | PreToolUse (block) | **Feasible** — `PreToolUse` on Bash, `tool_input.command`, `permissionDecision:"deny"` or exit 2 | **Feasible** — `PreToolUse`, input is `toolCall.args.CommandLine` (field name differs), matcher `"run_command"`, `{"decision":"deny"}` | **Feasible** — `tool.execute.before`, check `tool==="bash"`, inspect `output.args.command`, `throw new Error()` to abort |
| 3 | `claude_tk_standalone.sh` | PreToolUse (block, shlex-parsed) | **Feasible** — same mechanism as #2, shell out to the existing `claude_tk_standalone_check.py` unchanged | **Feasible** — same as #2, re-point the checker at `toolCall.args.CommandLine` | **Feasible** — `Bun.spawnSync` the existing python checker inside `tool.execute.before`, throw on nonzero |
| 4 | `claude_require_steps_pretool.sh` | PreToolUse (**soft**-block via `additionalContext`) | **Feasible** — `PreToolUse` supports `additionalContext`, exact parity | **Infeasible as a soft nudge** — agy's `PreToolUse` output is decision-only (allow/deny/ask/force_ask), no context-injection field at all (confirmed via agy's own "Current Limitations" doc). Nearest substitute, `PreInvocation`'s `injectSteps`, fires once per model turn with no tool-name matcher — loses the "skip Read/Glob/Grep" selectivity | **Infeasible cleanly** — `tool.execute.before` only exposes `output.args` (mutable tool input), no side-channel to inject text while letting the call proceed; only intervention is `throw`, which is a hard block, not a nudge |
| 5 | `claude_open_tickets_reminder.sh` | UserPromptSubmit (inject reminder) | **Feasible** — real `UserPromptSubmit` event, `prompt` field, direct parity | **Partial** — no `UserPromptSubmit` event; substitute `PreInvocation`'s `injectSteps` (fires before every model call in the loop, not exclusively at prompt submission — close but not identical) | **Feasible** — `chat.message` hook is literally "called when a new message is received," push an extra text `Part` into `output.parts`. Clean 1:1 match |
| 6 | `claude_open_tickets_stop_nudge.sh` | Stop (non-blocking notice) | **Feasible** — real `Stop` event, exit 0 without `decision`/`continue:false` to stay non-blocking | **Feasible** — `Stop` event exists, omit `decision:"continue"` | **Feasible** — no dedicated Stop hook name, but `mngr_opencode_plugin.ts` already handles `session.idle` via the `event` hook; put the same log logic there |

**RESOLVED — full per-tool-call parity achieved for all three, implemented
(not just designed).** The earlier "structural gap, fold into hook #5"
conclusion below was premature — it only checked pre-execution hooks.
Checking post-execution hooks found real mechanisms for both harnesses:

- **codex**: direct port, same mechanism as claude
  (`hookSpecificOutput.additionalContext`). Implemented at
  `scripts/codex_require_steps_pretool.sh` + `.codex/hooks.json`.
- **opencode**: `tool.execute.after` gives both the tool name AND a mutable
  `output.output` (the result text the model reads next) in one hook —
  genuine 1:1 parity, not a compromise. Implemented at
  `.opencode/plugin/require-steps.ts`. Verified: valid TypeScript (esbuild,
  zero errors) and the exact Bun Shell API chain used
  (`.quiet().nothrow().env().stdout`) live-tested with real Bun, confirmed
  working.
- **antigravity**: no single hook has both the tool name and an
  injection channel, so this needed two cooperating hooks instead of one —
  `PostToolUse` (has the tool name, output must be `{}`, so it can only
  leave a flag) feeding `PostInvocation` (can't see the tool name, but
  supports `injectSteps`/`ephemeralMessage`) via a small state file under
  `.tickets/`. Same pattern `mngr_antigravity`'s own plugin already uses
  internally for conversation tracking, not a novel hack. Implemented at
  `scripts/antigravity_flag_missing_step_posttooluse.sh` +
  `scripts/antigravity_require_steps_postinvocation.sh` + `.agents/hooks.json`.
  **Known limitation, not fixed by this**: agy's full tool taxonomy is still
  unenumerable, so only the one confirmed tool name (`run_command`) is
  treated as substantive — other real tools that should require a step
  (if any exist beyond run_command) won't trigger this hook until more
  tool names are confirmed.

All bash scripts syntax-checked (`bash -n`), all JSON hook configs
validated. None of the three has been exercised against a real running
agent session (no live codex/agy/opencode hook-firing test in this pass) —
review-and-static-validation only, same caveat as any hook config until a
real workspace runs one.

Also flagging again since it resurfaced independently in this pass: mngr's
own code comments record that agy's documented `PreToolUse
{"decision":"allow"}` does **not** actually suppress the run_command
confirmation dialog in practice (verified live
against agy 1.0.3) — even where an agy hook event exists on paper, verify
its real behavior against your installed build before trusting it.

### B. Config keys (`.claude/settings.json`, non-hook)

| Concept (source: `.claude/settings.json`) | Codex | Antigravity (agy) | OpenCode |
|---|---|---|---|
| `model` | `model` in `~/.codex/config.toml` | `model` in `settings.json`, but a **display-name string** (e.g. `"Gemini 3.5 Flash (Medium)"`), not a slug — must match `agy models` output exactly | `model` in `opencode.json`, format `"provider/model"` |
| `effortLevel` | `model_reasoning_effort` in `config.toml` (`minimal\|low\|medium\|high\|xhigh`) — same name mngr's plugin already uses | **NO EQUIVALENT key** — effort is folded into the `model` display-name string itself (the `"(Medium)"` suffix); setting effort means picking a differently-named model, not a separate field | `reasoningEffort`, but scoped **per-agent** in `opencode.json` (`agent.<name>.reasoningEffort`), not top-level |
| `statusLine` | `tui.status_line` in `config.toml` — an ordered list of built-in footer-item identifiers (config-driven picker), **not** an arbitrary shell command like Claude's | **Exact equivalent** — same shape as Claude: `statusLine: {"type":"command","command":"<shell>"}`, agy pipes JSON on stdin on every state change | **NO EQUIVALENT** — repeatedly requested upstream (GH #30295, #8619, #23539), not implemented; TUI status bar is hardcoded |
| `autoMemoryDirectory` | Not a directory setting — `memories.use_memories`/`memories.generate_memories` booleans gate OpenAI-*hosted* memory, no local configurable path | **NO EQUIVALENT config key** — internal memory-like state exists (`brain/`, `knowledge/`, `conversation_summaries.db`, confirmed present on a real install) but the location is fixed/internal, not exposed as a setting | **NO EQUIVALENT** — sessions persist in `opencode.db` (resume, not "memory"); cross-session memory only via third-party plugins (Mem0, Supermemory, Hindsight) |
| `enabledPlugins` / `extraKnownMarketplaces` (`imbue-code-guardian`, `frontend-design`) | **CORRECTED — real equivalent exists, earlier "NO EQUIVALENT" claim in this doc was wrong.** Codex has a genuine curated plugin marketplace: `/plugins` in the CLI browses "Curated by OpenAI"/"Shared with you"/"Created by you" categories with a real install button (confirmed via developers.openai.com/codex/plugins, not assumed). A plugin bundles skills + app integrations + MCP servers. No code-guardian-equivalent plugin exists for codex today — would need building/publishing, same as antigravity — but the *distribution mechanism* is real and comparable to Claude's, not absent. | **Real equivalent, differently shaped** — `agy plugin install <target>@marketplace` / `agy plugin link` genuinely support a marketplace concept; local enablement via `.agents/plugins.json`. But no code-guardian-equivalent plugin exists for agy today — would need building from scratch even with the mechanism available | `plugin` array in `opencode.json` — sources from **npm**, not a curated marketplace; closest analog is "list of npm packages to load," no equivalent to code-guardian exists today |

### C. Instruction file (`CLAUDE.md`)

Confirmed instruction-file convention per harness (all four use a flat file
at project root): claude → `CLAUDE.md`, **codex → `AGENTS.md`** (confirmed,
`model_instructions_file` config key can override), **antigravity →
`AGENTS.md`** (same filename as codex, confirmed via agy's own guide skill),
**opencode → `AGENTS.md`** (confirmed, described in its docs the same way
Cursor/codex use it). So three of the four converge on the same filename —
only claude is the odd one out here.

Content-wise, `CLAUDE.md` splits into:
- **Harness-neutral** (most of it): git conventions, testing/ratchets,
  services/supervisord, skill-authoring rules, `tk` task-management
  protocol itself. Copies verbatim into each `AGENTS.md`.
- **Harness-specific delta**: which built-in tool is disabled/replaced by
  `tk` (claude: `TodoWrite`; codex: `update_plan`, no disable flag exists —
  the instruction has to be advisory, not backed by a real block, matching
  `SYSTEM_PROMPT_NOTES.md`'s codex stopgap; opencode: `todowrite`, **does**
  have a real disable flag (`tools.todowrite=false`), so the instruction
  can say "this tool is disabled" truthfully; antigravity: unknown tool
  name, same caveat as codex but without even knowing what to name in the
  instruction).
- **"Claude's built-in memory system" reference** — no direct equivalent
  in any of the three (see `autoMemoryDirectory` row above); this sentence
  either gets dropped for non-claude harnesses or replaced with a custom
  `runtime/memory/`-style convention implemented from scratch (opencode has
  third-party plugin precedent for this, e.g. `opencode-agent-memory`;
  codex and antigravity would need something bespoke).

### D. Skills (`.agents/skills/`, symlinked to `.claude/skills/`)

Good news, confirmed in the earlier audit: skill **content** is already
harness-agnostic — of 23 skills, none reference `TodoWrite`, Claude's Task
tool, or `.claude/`-specific mechanics in their prose. **All four harnesses
confirmed to have a skills-loading concept, and opencode needs zero work at
all**: per opencode.ai/docs/skills/, opencode natively discovers skills from
`.agents/skills/<name>/SKILL.md` (and `.claude/skills/` too) — the exact
path fct already uses, deliberately built for Claude-Code compatibility.
Codex also confirmed (`skills.config` on its subagent config,
`skill_mcp_dependency_install` feature flag). Antigravity confirmed
(`builtin/skills/` directory structure present on a live install). So: no
duplication needed for skills at all, across any of the four harnesses —
this whole category is already solved by fct's existing directory layout.

### E. MCP servers

**Nothing to migrate** — confirmed via full-repo search, fct configures
zero MCP servers today (no `.mcp.json`, no `mcpServers` key anywhere). If
one gets added later, each harness's native format differs: codex →
`~/.codex/config.toml`'s `[mcp_servers.<name>]` tables; opencode →
`opencode.json`'s top-level `mcp` key (`type: "local"|"remote"`);
antigravity → has a confirmed `mcp` permission-action type (so it does
support MCP tool calls) but the server-*registration* file format wasn't
located in this pass. None of the three vendored mngr plugins currently
thread MCP config through as a typed field — would go through
`config_overrides`/`settings_overrides` (the existing raw-dict escape
hatch) for now.

### What's genuinely untranslatable — explicit callouts

- **Hook #4's soft-nudge mechanism** — structurally absent in both
  antigravity and opencode, not just hard to build. Requires a design
  decision (redesign as hard block, or drop in favor of #5), not more
  engineering effort.
- **`enabledPlugins`/`extraKnownMarketplaces` (code-guardian review bot)**
  — no equivalent *automation* (i.e. code-guardian itself) exists for any
  of the three today, but the *distribution mechanism* it would need is
  real for two of three: codex has a genuine curated plugin marketplace
  (`/plugins` in the CLI — corrected above, an earlier pass of this doc
  wrongly said codex had none) and antigravity has its own marketplace
  (`agy plugin install ... @marketplace`). Only opencode lacks a curated
  marketplace (npm-sourced `plugin` array only). See
  `changelog/multi-harness-support.md` for the trigger+gate-check work
  already done (codex real & tested, antigravity mechanism confirmed
  compatible, opencode via a workaround) and what's still unbuilt (the
  actual review skills, and — now that it's known to be real — genuinely
  publishing this as an installable plugin for codex/antigravity rather
  than embedding hook scripts directly in fct).
- ~~`autoMemoryDirectory`~~ — **resolved**, not a real gap: replaced with a
  shared `memory` MCP server all four harnesses (claude included) connect
  to, rather than trying to replicate claude's proprietary feature per
  harness. See `changelog/multi-harness-support.md`.
- **`statusLine`** — real equivalent for antigravity, real-but-differently-shaped
  for codex (a picker, not a shell command), **does not exist at all** for
  opencode (open upstream feature request).
- **`effortLevel`** — antigravity has no separate effort setting; effort is
  baked into which model *name* you pick, a meaningfully different UX than
  a boolean/enum toggle.
- **Antigravity's built-in tool names** — still genuinely unenumerable
  after two research passes (checked `--help`, plugin docs, builtin skills
  dir, live settings.json). Blocks not just hook #4 but any future
  disallow-list-style enforcement for antigravity specifically.

**Recommended build-system shape**, given the above: not a single
"compile skills" step (skills don't need it), but two narrower things:

- A small generator (`scripts/generate_harness_docs.py` or similar) that
  takes ONE source file with harness-conditional sections and emits
  `CLAUDE.md` + each harness's instruction-file equivalent (`AGENTS.md` for
  codex — confirmed to be codex's real convention; need to confirm
  antigravity's and opencode's equivalents before building this).
  Committed-output (run once, check in the results), not
  provision-time-generated — this content changes rarely and committed
  output is auditable in review, unlike something regenerated silently on
  every container boot.
- Hand-written, per-harness hook/plugin implementations for the
  `tk`-enforcement logic — no way around this being real, separate code per
  harness, given the structurally different hook mechanisms. Budget this as
  the largest chunk of Phase 3.

## Phase 1-3: full workflow-backed code review (xhigh), fixed

At the user's explicit request, a full multi-agent code review (6 finders +
verify + synthesis) ran against the entire Phase 1-3 diff in both
forever-claude-template and mngr. 15 real, distinct, verified defects were
found and all 15 fixed in the same pass (not just reported) — three of them
were regressions on previously-working claude functionality (template
renames breaking system_interface's UI and 6 delegation skills, a removed
default breaking the minds desktop client's create-workspace flow), one was
a real settings.toml-can't-even-parse blocker (mngr CLI plugin
registration + stale uv.lock), and one was a separate data-loss bug found
along the way (an earlier scratch-git-branch test in this session had
silently discarded Dockerfile edits). Full detail, fix-by-fix, in
`changelog/multi-harness-support.md`'s code-review entry. One finding (a
memory-MCP-tool-call reminder hook) was deliberately left unbuilt rather
than rushed, since it's a design question with no checkable state, not a
mechanical bug — documented there as an explicit open decision.

## Phase 3 status (updated as work lands)

- **Hooks**: 5 of 6 done and real-tested — #2 (prevent-commit-rewrite), #3
  (tk-standalone), #4 (require-steps, full per-tool-call parity), #5
  (open-tickets reminder), #6 (open-tickets stop-nudge), all across
  codex/antigravity/opencode. codex and opencode ports for #5 are direct
  (confirmed plain-stdout support for codex; opencode needed a different,
  *experimental*-namespaced hook than first assumed — `chat.message`
  doesn't actually expose mutable content, `experimental.chat.messages.transform`
  does, with an unconfirmed exactly-once-per-turn firing guarantee, guarded
  defensively with an in-memory per-message dedup). antigravity's #5 uses
  `PreInvocation`/`injectSteps`, same real mechanism validated for #4,
  simpler here (one hook, not two, since #5 doesn't need PostToolUse's
  tool-name visibility). **#1** (plugin auto-update) remains not built —
  previously written off as moot (nothing to install for non-claude
  harnesses), which is now stale given real code-guardian plugin variants
  exist (see below) — worth reopening, not yet done.
- **MCP / memory**: done — shared server across all four harnesses,
  including a real upstream `mngr_antigravity` fix.
- **Skills**: done, zero work needed.
- **Config keys**: `autoMemoryDirectory` resolved via the MCP work above.
  `model` set for all three (codex `gpt-5.5`, antigravity `Gemini 3.5 Flash
  (High)` — Flash-tier, not Pro-tier: "Gemini 3.5 Pro" isn't a real
  selectable value, only "Gemini 3.5 Flash" and "Gemini 3.1 Pro" are —,
  opencode `openrouter/z-ai/glm-5.2` — all three confirmed live via
  each harness's own CLI, not guessed). `model_reasoning_effort` set for
  codex (`xhigh`); antigravity has no separate effort field (folded into
  the model display name); opencode has **no `reasoningEffort` field at
  all** — fetched the real, current config schema directly and confirmed
  this, correcting an earlier wrong claim in this doc that it existed.
  `statusLine` skipped (low-value, cosmetic only, explicit call). Real,
  disclosed gap: opencode's `glm-5.2` routes through OpenRouter, a separate
  paid API with no credential wiring found anywhere in this repo — the user
  will provide keys separately. `enabledPlugins` not wired — blocked on the
  code-guardian plugin PR (below) actually merging first.
- **`AGENTS.md`**: done for codex and opencode, and now also functional for
  antigravity via a real code fix (below) — but the file itself is not
  simply "read by all three" the way an earlier pass of this doc claimed.
  **Correction, caught by direct challenge, not caught proactively**:
  codex confirmed via its own docs ("Codex reads AGENTS.md files before
  doing any work", precedence-ordered discovery) and opencode confirmed
  via its own docs (`AGENTS.md` first in its file-resolution precedence,
  "traversing up from current directory") both genuinely auto-discover
  `AGENTS.md` with zero configuration. **Antigravity does not** — it
  natively reads only a single GLOBAL `$HOME/.gemini/GEMINI.md`, no
  per-project file convention of its own at all. Real fix, not a
  workaround-and-hope: added `global_instructions_md` to
  `AntigravityAgentConfig` (`mngr_antigravity/plugin.py` +
  `antigravity_config.py`, same pattern as the earlier `mcp_servers` fix —
  a new field writing to a path nothing wrote to before), wired in fct's
  `.mngr/settings.toml` to seed agy's `GEMINI.md` with a rule telling it to
  also check the project workspace for `AGENTS.md` (the documented
  real-world workaround, not invented here). 2 new tests added
  (write-when-set, no-write-when-unset), full suite re-run: 133 passed
  (131 + 2), synced into `vendor/mngr` after confirming the diff was
  exactly this change and nothing else.

  `AGENTS.md`'s own content: copied from `CLAUDE.md` with the genuinely
  Claude-specific bits adjusted (the disabled-built-in-todo-tool framing,
  since enforcement differs per harness; self-references; the skills
  symlink aside, which was Claude-implementation detail not relevant to
  the others). The two files now cross-reference each other in their
  self-modification sections so a shared-content change gets made in both.

  **CORRECTION (2026-07-10, superseding the paragraph above):** the
  `global_instructions_md`/`GEMINI.md`-relay approach described above was
  wrong and has been fully removed (mngr commit `8031b80`, fct vendor copy
  synced). Direct inspection of the real installed `agy` binary (`strings`
  on the binary, per explicit user instruction to verify against real CLIs
  rather than assume) showed agy already natively discovers `AGENTS.md`
  walking up from cwd to repo root, same as codex/opencode — no relay
  needed at all. This was the **second** time this exact wrong claim was
  made and "fixed" in this project; the first "fix" (earlier in the same
  session, described in the paragraph above) was itself never checked
  against the real binary either. No fct-side code is needed for
  antigravity's `AGENTS.md` discovery — it just works, same as the other
  two. See `changelog/multi-harness-support.md`'s "Correction:
  antigravity's `global_instructions_md` was never needed" entry for full
  detail.
- **code-guardian** (adjacent, not formally in Phase 3): reverted an
  earlier attempt to embed a port directly in fct. Instead opened
  https://github.com/imbue-ai/code-guardian/pull/25 (draft) adding real
  codex/antigravity/opencode plugin variants via each harness's own
  idiomatic distribution channel (codex: real curated marketplace,
  confirmed via docs; antigravity: `agy plugin install`/`link`, structure
  validated against a real local `agy` install; opencode: npm, matching its
  documented convention). Scope: enforcement-gate only, not the actual
  autofix/verify-conversation/verify-architecture skills, which are
  genuinely Claude-Code-specific orchestration needing a per-harness
  rewrite, not a port. fct's own side stays a one-line config pointer,
  unchanged from before, once that PR lands.

---

## Phase 4 — DONE

`apps/system_interface`'s backend — `agent_discovery.py` (resolved
`CLAUDE_CONFIG_DIR`, fell back to `.claude` explicitly), `claude_auth.py`/
`claude_auth_endpoints.py`/`claude_auth_patterns.py`, `session_parser.py`
(parsed Claude's JSONL transcript format specifically) — all assumed
claude. Until these had per-harness equivalents, the minds UI could not
list, show status for, or render the transcript of a codex/antigravity/
opencode agent, even if one was successfully created via `mngr create`.
Landed in three pieces:

1. **List/status** — `harness.py` (`Harness` enum, `parse_harness`) threaded
   through `agent_discovery.py`/`agent_manager.py`/`server.py`, so every
   agent-facing response now reports its harness.
2. **Transcript** — `AgentSessionWatcher` stays Claude-only (rich: subagent
   linkage, byte-offset paging); a new `CommonTranscriptWatcher` covers the
   other three by reading mngr's own already-existing, already-written
   common-transcript output (`imbue.mngr.agents.common_transcript_records`)
   instead of duplicating mngr's per-harness CLI-wrapping work. Routed via
   `transcript_watcher.build_transcript_watcher(agent_info, on_events)`.
   Live-verified against real `codex`/`opencode` CLI runs (mngr's own
   `pytest.mark.release` e2e tests) — see changelog for the codex-auth
   caveat hit along the way.
3. **Auth status** — read-only per-harness auth-error *detection* (mirrors
   `claude_auth_patterns.py`, seeded with codex's live-confirmed error text)
   feeding the same `is_auth_error` signal the frontend already had, plus a
   frontend fix so the Claude-specific OAuth recovery modal (which only
   knows how to drive `claude auth login`) can no longer be triggered by a
   non-claude agent's auth failure. This is detection/signal-safety only —
   the full interactive recovery flow (a login modal that can actually walk
   a user through codex/antigravity/opencode's own auth) is genuinely new,
   harness-specific product surface and belongs in Phase 5 below, not here.

See `changelog/multi-harness-support.md` for the full detail, test counts,
and the explicit gaps (antigravity/opencode have no auth-error patterns
seeded yet; codex's headless `exec` failure mode — a silent empty
completion with no assistant text at all — isn't caught by text matching).

---

## Phase 5 — Authentication UX

This is genuinely new infrastructure, not an extension of something that
already generalizes. Key findings from research, stated plainly:

- **`mngr_latchkey` is not a model-provider credential system.** It's a
  scoped proxy for *third-party tool services* an agent calls out to (Slack,
  GitHub, Gmail) — completely unrelated to authenticating the coding-agent
  CLI itself. Don't reach for it here.
- **No multi-provider-account concept exists anywhere in mngr or minds.**
  Every plugin assumes one human, one login per CLI, shared via symlink to
  every agent on that host. Minds itself caches no credentials locally
  today — Claude's own API key is typed fresh into the Create form every
  time (`Create.jinja:652`), and "subscription" mode does nothing
  automated at all.
- **The good news: a working manual pattern for OAuth-based harnesses
  already exists in production, just not generalized.** Claude's
  "SUBSCRIPTION" auth mode today is exactly "open the live in-browser
  terminal for this workspace, let the user run the login command
  themselves, they complete OAuth in their own browser." This is the same
  thing we ourselves did with antigravity earlier — no relay needed, the
  live terminal *is* the relay. This is the pattern to generalize, not
  build from scratch.

**Concrete pieces, mapped to your three UX asks:**

1. **New-workspace Create form — multi-harness auth + default-harness
   picker.** Requires a real data-model change: today it's one
   `ai_provider`/`anthropic_api_key` pair per workspace
   (`primitives.py:72-89`, `Create.jinja`). Needs to become N entries, one
   per harness the user wants available, each with its own mode
   (API key / subscription-via-live-terminal / IMBUE_CLOUD-style minted
   key where that concept applies), plus a "which harness does the
   `/welcome` agent use" selector — this is also exactly what
   `FCT_HARNESS` on the `main_<harness>` create-template overlay
   consumes, so this UI field maps directly to a `.mngr/settings.toml`
   concept that already exists.
2. **"+" / New Agent menu — harness picker scoped to already-authenticated
   harnesses.** Lives in `apps/system_interface` (`DockviewWorkspace.ts`,
   `CreateAgentModal`, `agent_manager.py`), not in minds itself — currently
   has no agent-type concept at all (`_build_chat_create_command`
   hardcodes `--template chat`). Needs: (a) the harness-name dropdown, (b)
   a query against wherever Phase-5-item-4's credential state lives to
   filter to authenticated harnesses only, (c) `agent_manager.py` passing
   the chosen harness through to `--template chat_<harness>`/
   `worktree_<harness>` instead of the hardcoded claude template.
3. **"+" menu — add authentication for a new harness (API key or
   subscription).** Two different implementations depending on mode:
   - *API key* (codex, opencode both support this): straightforward — a
     form field, stored wherever the new credential-vault lives (see next
     item), injected as an env var at agent-creation time the same way
     `anthropic_api_key` is today.
   - *Subscription/OAuth* (antigravity always; claude/codex optionally):
     generalize the existing live-terminal pattern into a first-class flow
     — clicking "authenticate" opens a live terminal session running that
     harness's login command, minds polls for the resulting credential
     file to appear (each plugin already knows its own expected path:
     `~/.codex/auth.json`, `~/.gemini/.../antigravity-oauth-token`,
     `~/.local/share/opencode/auth.json`), and marks the harness
     authenticated once detected. No new relay infrastructure needed —
     this reuses the terminal-tunneling minds already has.
4. **Credential persistence — the actual new subsystem.** Today nothing
   persists credentials across workspaces at the minds-user level (only
   IMBUE_CLOUD mode has any server-side secret material, minted fresh per
   workspace). Your design implies "authenticate once, available for
   future new agents" — that needs a real per-(minds-user, harness)
   credential store, referenced at every future agent-creation regardless
   of which host/workspace. This is the largest genuinely-new piece of
   Phase 5 and should be scoped/designed as its own sub-project before
   the UI work in items 1-3 is built on top of it, since all three UI
   pieces depend on where this state actually lives.

---

## Phase 6 — DONE

`create_worker.py`/`launch-task` needed a `--harness` arg or equivalent so
delegated sub-agents weren't always claude regardless of which top-level
agent initiated them. Landed without a new CLI flag or any change to the
six delegation skills' prose: `.mngr/settings.toml` already had
`worker_<harness>`/`subskill-worker_<harness>` templates for all four
harnesses (built earlier, never wired to anything), so `create_worker.py`
now auto-detects the *delegating* agent's own harness from its
`data.json`'s `type` field (`_resolve_delegating_harness`, mirroring
`harness.py`'s `parse_harness` suffix-stripping -- duplicated, not
imported, since this is a standalone `uv run --script` file with no
dependency on the system_interface package) and suffixes whatever template
the caller requested (`_resolve_template`). A codex lead delegating a
worker now gets `worker_codex`; an undetectable harness (no `data.json`,
tests, non-mngr envs) falls back to the bare template unchanged -- today's
behavior, preserved.

Live-verified: all six new template names (`worker_codex/antigravity/
opencode`, `subskill-worker_codex/antigravity/opencode`) resolve via
`mngr config get create_templates.<name>` with the correct `type` field.
Tests: 19 new cases in `create_worker_test.py` (65 passed total, up from
46), `mypy` clean.

---

## Phase 7 — DONE

The last always-claude creation path: the "+" button's "New chat"/"New
agent" menu items (user-initiated, not delegated -- Phase 6's counterpart).
Scoped explicitly to creation only, per the user's instruction to assume
the underlying host is already authenticated for whichever harness is
picked; Phase 5's interactive auth-recovery UI remains untouched.

The dropdown items are no longer directly clickable (ambiguous which
harness) and instead expand a hover submenu (Claude/Codex/Antigravity/
Opencode); picking one opens the existing name-input modal with that
harness pre-selected and threaded through to `create_chat_agent`/
`create_worktree_agent`, which now resolve `chat_<harness>`/
`worktree_<harness>` templates (same pre-existing-but-unwired templates
pattern as Phase 6) instead of the bare claude-aliased ones. Also fixed a
narrow but real bug found along the way: `_run_creation` hardcoded
`harness=Harness.CLAUDE` on the resulting `AgentStateItem` regardless of
what was actually requested -- now reads it from the caller.

Verified directly against a real backend/build rather than tests alone
(`npm run build`, a booted Flask server, live `curl` requests) after
finding the `update-system-interface` skill's "never edit the served tree"
rule didn't fit this session (template-repo dev, not a live deployed
mind) -- confirmed with the user before proceeding. Could not verify the
hover submenu visually (no browser tooling available this session);
flagged as a real gap rather than skipped silently. See
`changelog/multi-harness-support.md` for full detail.

---

## Live create-testing pass (started 2026-07-09, IN PROGRESS — real blocking bug, unresolved)

Everything above (Phases 1-7) was built and reviewed but, until this pass,
never taken through a real `mngr create` end-to-end for anything but
docker. This pass is that: actually creating real agents against real
providers and fixing what breaks. **Pick up here.**

### Branch / repo state

- `imbue-ai/forever-claude-template@multi-harness-support` — pushed, latest
  commit `6cedb231`.
- `imbue-ai/mngr@multi-harness-support` — pushed, latest commit `8031b80`.
- **These two branches are mutually required.** fct's Docker/Modal build
  does an `ADD https://api.github.com/repos/imbue-ai/mngr/git/refs/heads/<branch>`
  instruction during build (a cache-busting ref-check against the live
  GitHub branch, exact downstream use not fully traced this session) — this
  404s if the mngr-side branch doesn't exist upstream. If you branch either
  repo again (e.g. rename `multi-harness-support`), branch the other one to
  match and push both, or this same 404 recurs.
- Minds environment this was tested against: **`minds-staging`** (Modal
  environment name `minds-staging-2925fe0a3b6b4fe1ba1f5b1beb98104b`), not
  the local `minds-eval-box` container — this answers an earlier open
  question in this project about which orchestrator was stale.

### Verification standard (explicit, repeated user instruction — follow this)

Do not guess CLI flags, config schemas, or provider behavior from memory or
by pattern-matching a sibling provider's config. Check against the real
installed CLI binaries (`agy`, `codex`, `opencode`, `mngr`) or real source
before shipping a config change. Specifically: `mngr config get <path>`
**only** validates that a TOML key/value resolves against the generic
`CreateTemplate` Pydantic schema — it does **not** validate the *contents*
of list/string fields (like `build_arg`) against whatever provider-specific
parser eventually consumes them. That exact gap shipped a real bug this
session (bug 3 below); `mngr config get` said it was fine, a real `mngr
create` proved it wasn't. Where possible, actually execute the real parser
function against your exact configured value (see bug 3's fix for the
pattern: extract/import the real function, feed it the literal value going
into `settings.toml`, assert on the result) rather than reasoning about it
from reading the source.

### Bugs found and fixed this session (real `mngr create` failures, not review findings)

1. **Antigravity `global_instructions_md`** — added, then found wrong via
   direct `agy` binary inspection, fully removed. See the CORRECTION note
   inline in Phase 3 status above for full detail. **Second time this exact
   wrong claim was shipped in this project** — first "fix" was also never
   checked against the real binary.
2. **Missing `[create_templates.modal]`** in fct's `.mngr/settings.toml` —
   `mngr create --template modal` failed with "Template 'modal' not found."
   Added, modeled on vultr/aws (`agent_creator.py`'s own comment: "same
   remote shape"), minus their VM-specific `start_arg`/
   `post_host_create_outer_command` fields (Modal has no persistent
   VM/SSH layer to reboot-recover). Verified only via `mngr config get`
   at the time — insufficient, see bug 3.
3. **Modal `build_arg__extend` used docker's bare `"."` positional** —
   `["--file=Dockerfile", "."]`, copied from docker/vultr/aws where `"."`
   is the real `docker build -f Dockerfile .` CLI convention. Modal's own
   parser (`mngr_modal/instance.py::_parse_build_args`, argparse-based) has
   **no positional argument at all**, only named flags — the bare `"."`
   was rejected: `Error: Unknown build arguments: ['.']`. Fixed by
   dropping it entirely (not translating to `--context-dir=.`): when
   `--context-dir` is omitted, `_get_modal_image_definition`
   (`instance.py:902`) already defaults it to the Dockerfile's own parent
   directory, which is `.` here since `--file=Dockerfile` is a bare
   relative filename — one less thing to get wrong later. Verified for
   real, not just `mngr config get`: extracted `_parse_build_args`'s exact
   logic and ran it standalone against old/new values (reproduced the
   precise user-reported error on old, clean pass on new, matching
   effective context dir), then re-ran through the real
   `ModalProviderInstance._parse_build_args` via its own mocked test
   fixture (`make_modal_provider_with_mocks`) — 44 existing unit tests
   plus one new ad-hoc check against the literal `settings.toml` value,
   all pass. Committed as fct `1baaf912` (fix) + `6cedb231` (changelog).

### CURRENT BLOCKING BUG — unresolved, start here

After fixing bug 3, a real `mngr create --template modal` against
`minds-staging` got much further — past build-arg parsing, through
installing claude/codex/antigravity/opencode CLIs, through `uv sync`
(181 + 68 + 86 + 19 packages across the various pyproject layers), through
`scripts/build_workspace.sh` (frontend build + all workspace packages) —
but failed on the **last** image layer:

```
Building image im-gR1Ep1HkLkwFBKdeGUbFp8
=> Step 0: FROM base
=> Step 1: COPY scripts/fct_seed.sh /usr/local/bin/fct-seed
Built image im-gR1Ep1HkLkwFBKdeGUbFp8 in 4.43s

Building image im-whSbrm85dBPhMam5Vq8e6s
=> Step 0: FROM base
=> Step 1: RUN chmod +x /usr/local/bin/fct-seed
running container: starting container: starting root container: starting sandbox: failed to create process working directory "/mngr/code/": failed to create directory "/mngr/code/": file exists
Terminating task due to error: failed to run builder command "chmod +x /usr/local/bin/fct-seed": container exit status: 128
Error: Failed to create Modal sandbox: Image build for im-whSbrm85dBPhMam5Vq8e6s failed.
```

Context that matters for diagnosis: an earlier layer in the same build
(`im-buA1oIeeIFQgTuiOB94gFj`, `RUN mv /mngr/code /docker_build_code`, took
64s) relocates the build output before final assembly — presumably a
convention shared with docker/vultr/aws's Dockerfile stages, not modal-
specific. `WORKDIR /mngr/code/` was set many layers earlier
(`im-zcCgVkyfGuyrhjPsoxWYAB`). Modal's build log shows it materializes
**every single Dockerfile instruction as its own chained Sandbox** (`=>
Step 0: FROM base` / `=> Step 1: <one instruction>` / `Saving image...`,
repeated ~40 times across this build) — i.e. each layer boots a fresh
sandbox from the previous saved image and runs exactly one instruction.
The failure is that instruction-sandbox's own creation logic trying to
(re)materialize `/mngr/code/` as the new sandbox's process working
directory (because `WORKDIR` still points there from many layers back) and
finding the path already occupied — immediately after the prior layer
moved the original directory away with `mv`.

Not yet root-caused. Hypotheses, in the order worth checking:
1. Modal's per-layer-cached sandbox snapshotting doesn't correctly carry
   forward a `mv`'d-away directory's absence into the next layer's sandbox
   (a caching/snapshot bug or an artifact of how `_build_image_from_
   dockerfile_contents` applies each instruction — re-read that function
   in full, not just the `context_dir` logic already read this session, at
   `libs/mngr_modal/imbue/mngr_modal/instance.py` around lines 860-910 and
   wherever the per-instruction sandbox loop itself lives).
2. Docker/vultr/aws build as one continuous container (real `docker build`
   or equivalent), so a mid-Dockerfile `mv` of the WORKDIR target has never
   been exercised against Modal's structurally different per-layer-sandbox
   model before now — this may be a genuine, previously-undiscovered
   incompatibility between the shared Dockerfile and Modal's build
   mechanism, not a simple config mistake like bugs 1-3 above.
3. If so, the fix is likely either: a Modal-specific adjustment to
   `_build_image_from_dockerfile_contents`'s WORKDIR-recreation step (skip
   recreating it if the parent already resolved a `mv` away from it), or
   restructuring the shared Dockerfile so the `mv /mngr/code
   /docker_build_code` step doesn't leave a dangling `WORKDIR` reference at
   all (e.g. an explicit `WORKDIR /` before the `mv`, or moving the `mv` to
   the very last instruction so nothing after it depends on the old
   WORKDIR). Check whether docker/vultr/aws's Dockerfile stage even needs
   this `mv` step for modal specifically, or whether it can be skipped/
   reordered for modal only, before assuming a fix to shared code is
   required.

### Local docker-provider CLI prep (confirmed, not yet exercised)

Checked once, in this repo's own checkout, nothing further attempted:
Docker Desktop daemon is running locally (`docker info` succeeds); `uv
sync` in `~/imbue/forever-claude-template` produces a working local `mngr`
CLI at `.venv/bin/mngr` (`mngr 0.2.17`), confirming the editable
`vendor/mngr` install resolves and the CLI itself runs. **No real `mngr
create` has been attempted locally yet** — this is plumbing-only
confirmation that the tool exists and starts, not evidence any create
path works. `mngr create --help` confirms the exact flag shape needed:
`mngr create <name> --new-host --template main --template docker
--project <path> [...]`, matching `agent_creator.py`'s DOCKER-mode
command construction referenced above.

### Explicit authorization for the next agent (from the user, verbatim intent)

"You have permission to directly do `mngr create` with docker and test
everything until we have ported stuff over 1:1 to the other harnesses per
our plan document." Concretely:

- **Docker provider creates are pre-authorized**, repeatedly, for iterative
  testing — don't ask before running `mngr create --template docker
  --template docker_<harness>` (or whatever the correct template chain is
  per `agent_creator.py`) for any of the four harnesses. Docker is local,
  free, and the fast iteration loop for this work.
- **Cloud providers (modal/vultr/aws/imbue_cloud) are NOT blanket-
  authorized** for repeated real creates — those provision real, billed
  infrastructure. The modal create that surfaced the bug above was run by
  the user through minds' own UI, not something to keep re-triggering
  directly without asking. If the docker-provider pass reveals the same
  class of bug likely also affects a cloud provider, say so and ask before
  spending real cloud resources to confirm it.
- **Goal**: all four harnesses (claude/codex/antigravity/opencode) working
  1:1 through a real, live `mngr create` → running agent → renders
  correctly in minds' UI, per every phase above. Iterate on real failures
  as they surface, fixing each with the verification standard above (real
  code execution, not pattern-matched guesses) — this is exactly the kind
  of "test everything, don't guess" work the user has been asking for
  across this whole session.
- **Do not lose branch state again.** Commit and push incrementally as
  fixes land, on both repos as needed — the single biggest, most avoidable
  failure of this whole project so far was two separate sessions' worth of
  real work sitting uncommitted/unpushed until a live error surfaced it.

---

## What's NOT covered above, worth a decision before starting

- ~~`.reviewer/`/code-guardian automated review~~ — **decided and partly
  built**: not dropped. Trigger + gate-check ported and real-tested for
  codex; antigravity's mechanism confirmed compatible (not yet wired);
  opencode has a working real-code idle-time workaround. The actual review
  skills (autofix/verify-conversation/verify-architecture) are still
  unported — see `changelog/multi-harness-support.md`. Separately, now
  that codex is confirmed to have a real plugin marketplace (corrected
  above), genuinely publishing this as an installable plugin for codex and
  antigravity — instead of embedding hook scripts in fct — is worth
  deciding on as a follow-up, since it's a change to a different, shared
  repo (`imbue-ai/code-guardian`), not fct.
- Usage/cost tracking (`mngr_claude_usage`-style) for the other three, if
  the UI surfaces spend anywhere — each harness has its own `_usage`
  plugin already (`mngr_codex_usage` etc.) per mngr's own parity work, but
  wiring it into minds' UI is separate from wiring it into fct.
- ~~The `--message "/welcome"` skill-invocation question~~ — **resolved**.
  Checked each harness's real invocation convention rather than leaving it
  flagged: antigravity already worked (`/welcome`, confirmed same
  slash-from-skill-name convention as claude); codex needed `$welcome`
  (its real mention syntax, confirmed via docs — `/welcome` would have
  been meaningless text to it); opencode has no manual invocation
  mechanism at all, so it gets a plain-English nudge instead
  (`"Use the welcome skill to greet the user."`) — stated plainly as
  less deterministic than the other three, not a confirmed-equivalent
  fix. Implemented via `_WELCOME_MESSAGE_BY_HARNESS`/`_resolve_welcome_message()`
  in `libs/bootstrap/src/bootstrap/manager.py`, mirroring the existing
  `_resolve_chat_template()` pattern. Also fixed a stale pre-existing test
  found along the way (asserted the old pre-multi-harness `--template ==
  "chat"`) and added real parametrized coverage across all four harnesses
  — see `changelog/multi-harness-support.md`.
