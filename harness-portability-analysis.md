# Harness portability analysis

How tightly is this template coupled to Claude Code, and what would it take to make
other agent harnesses (Codex, Antigravity/Gemini CLI, pi, Hermes, …) work here as
well as Claude Code does?

This document has four parts:

1. **Inventory** — every place the repo is bound to Claude Code, with a severity grade.
2. **Landscape** — what is genuinely standardized across harnesses vs. what is irreducibly per-harness.
3. **Decision matrices** — coupling × portability, harness × capability, and roadblock classification.
4. **Verdict & paths forward** — which roadblocks are self-inflicted (our organization) vs. fundamental (the harnesses differ), and concrete options.

---

## TL;DR

- The coupling splits cleanly into **two halves**. The **content half** (skills, instruction prose, the delegation architecture, model/effort config) is *shallowly* coupled — names and formats differ across harnesses but the concepts map one-to-one. The **runtime/observability half** (hooks driving the `tk` progress view, and especially the `system_interface` web app that parses Claude's JSONL transcripts and drives Claude's auth) is *deeply* coupled.
- **Most of our coupling is self-inflicted, not fundamental** — but not all. The progress-view + tk-step machinery and the entire `system_interface` app were *designed against Claude Code's specific internals* (transcript JSONL schema, `additionalContext` injection, `transcript_path`, `.claude.json` auth internals). A different harness with equivalent primitives (Codex, Gemini CLI) could support them, but only after we build a per-harness adapter — work we never had to do because there was only ever one harness.
- **The field is converging on three real standards: MCP, AGENTS.md, and agentskills.io SKILL.md.** Everything we built on top of those (skills especially) is nearly free to port. Everything we built on top of *Claude-specific* surfaces (the plugin/marketplace system, the JSONL transcript schema, the auth flow, the scriptable status line) is where the cost lives.
- **The two hard, irreducible roadblocks are: (1) transcript formats** (no cross-harness standard; every harness persists differently — JSONL vs SQLite vs cloud vs markdown), which breaks the `system_interface` parser and any transcript-driven feature; **and (2) auth**, which is 100% per-vendor with no abstraction seam today.
- **Codex CLI and Gemini/Antigravity CLI are the two best targets** — both deliberately cloned Claude Code's hooks, skills, subagents, and headless model, so the extensibility surface ports with renames rather than redesigns. **pi and Aider are the worst fits** (pi deliberately omits MCP and subagents; Aider omits skills, hooks, and subagents entirely). **Hermes** is a maximal harness with its own everything (SQLite sessions, dual hook system) — capable but idiosyncratic.

---

## Part 1 — Inventory of Claude Code coupling

Grouped by layer. Severity = how hard to generalize: **Shallow** (rename/remap config), **Moderate** (needs an adapter but a clean seam exists), **Deep** (written directly against Claude internals, no seam).

### 1.1 Lifecycle hooks (`.claude/settings.json` + `scripts/claude_*.sh`)

Five hook surfaces: `SessionStart`, `PreToolUse`, `UserPromptSubmit`, `Stop`, `statusLine`.

| Hook | What it does | CC mechanism relied on | Severity |
|---|---|---|---|
| SessionStart (`uv sync`, `claude_update_plugin.sh`, `ensure_tk_on_path.sh`) | env setup, plugin install, put `tk` on PATH | run-on-start event; `claude plugin` CLI + `CLAUDE_CONFIG_DIR` cache | Shallow (except plugin install = Deep) |
| PreToolUse → `claude_prevent_commit_rewrite.sh` | block `git rebase/amend` | exit-2-blocks + stderr-to-model | Shallow |
| PreToolUse → `claude_tk_standalone.sh` | force `tk start/close` to be standalone commands | exit-2-blocks | Moderate (generic block, but exists only to protect the CC-transcript-derived progress view) |
| PreToolUse → `claude_require_steps_pretool.sh` | nudge agent to declare `tk` steps before working | **`hookSpecificOutput.additionalContext` JSON-stdout injection** (non-blocking) | **Deep** |
| PreToolUse → `claude_tk_close_reoutput_nudge.sh` | detect prose-before-close, nudge re-output | **`transcript_path` passed to hook** + `additionalContext` | **Deep** |
| UserPromptSubmit → `claude_open_tickets_reminder.sh` | remind about open steps | stdout-added-to-context | Shallow |
| Stop → cwd guard + `claude_open_tickets_stop_nudge.sh` | refuse stop outside repo root; nudge | exit-2-refuses-stop | Shallow |
| Stop → `detect_crystallization_candidate.py` (documented; wired via the code-guardian plugin, not in `settings.json`) | nudge crystallization after a heavy turn | Stop event + exit-2; reads mngr **common transcript**, deliberately *not* CC's `transcript_path` | Moderate (consciously de-coupled) |
| statusLine → `claude_status_line.sh` | render branch/PR status bar | `statusLine` run-command-render-stdout | Shallow (Missing on Codex/Gemini today) |

**Key finding:** the *hard-block* contract (exit 2 + stderr) is the most portable CC mechanism — Codex, Cursor, and Gemini CLI all copied it. The **least** portable mechanism is **non-blocking `additionalContext` injection** plus **`transcript_path`-to-hook**, which together power the soft tk-step reminders. A harness lacking a "inject context without blocking" channel can only hard-block or stay silent.

### 1.2 Skills, plugins, instruction file

| Surface | Binding | Severity |
|---|---|---|
| Skill **file format** (`.agents/skills/*/SKILL.md`, validated by `validate_skill.py` against agentskills.io) | **Cross-harness standard** — not CC-proprietary | Shallow |
| Skill **discovery** (`.claude/skills -> ../.agents/skills` symlink) + **invocation** (Skill tool, `/slash`, description auto-trigger) | CC-specific loader; the `.agents/skills` *path itself* is now read natively by Codex and Gemini | Moderate |
| `skills-lock.json` (vendoring external skills w/ hashes) | repo/mngr-level, not a CC file | Shallow |
| **Plugins/marketplaces** (`extraKnownMarketplaces`, `enabledPlugins`, `claude plugin` CLI, `imbue-code-guardian`, `frontend-design`) | **CC-only mechanism**; `imbue-code-guardian` ships slash-commands + a Stop hook the workflow depends on (`/autofix`, `/verify-conversation`) | **Deep** |
| `.reviewer/` config (issue taxonomies + settings) | mostly harness-neutral markdown/JSON data; delivery/invocation is CC plugin-bound | Moderate |
| `CLAUDE.md` (38 KB operating manual; **no `AGENTS.md` exists**) | CC auto-loads `CLAUDE.md`; body names CC-only constructs (TodoWrite, Skill tool, slash commands, `.claude/settings.json`, `autoMemoryDirectory`) | Moderate (filename portable to AGENTS.md; contents need rewrite) |
| Model/effort (`model: opus[1m]`, `effortLevel: high`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`) | CC config *spelling*; every concept is universal | Shallow |

**Key finding:** clean split between **content** (rides agentskills.io — portable) and **harness wiring** (the loader, the plugin system, the literal `CLAUDE.md` filename and its CC-specific prose — bound). The single heaviest item here is the **plugin/marketplace system + the code-guardian quality gate** (`/autofix`, `/verify-conversation` are mandatory in CLAUDE.md and are plugin-delivered).

### 1.3 `system_interface` web app — the deepest coupling

This app is effectively **a second client for Claude Code's private on-disk state**. It uses no API/SDK; it reads Claude's JSONL transcripts, marker files, and config files directly, and drives the `claude` CLI as a subprocess.

| Coupling area | What's assumed about Claude Code | Severity |
|---|---|---|
| **Transcript block schema** | `type ∈ {assistant,user,attachment}`; nested `message.content` typed blocks (`text`/`tool_use`/`tool_result`); `uuid`/`timestamp` required; Anthropic `usage` field names; tool literally named `Agent` for subagents; `toolUseResult.agentId` | **Deep** |
| **Literal control strings** | exact text matches for `[Request interrupted by user]`, `Continue from where you left off.`, `<synthetic>` + `No response requested.`, `queued_command`, stop-hook/skill-expansion/`/welcome` prefixes, `tk Updated -> status` | **Deep** (most likely to silently break on a CC update) |
| **On-disk layout** | `claude_session_id_history` sidecar; `<CLAUDE_CONFIG_DIR>/projects/**/<session_id>.jsonl`; subagents at `<sid>/subagents/agent-<id>.jsonl` + `.meta.json` | **Deep** |
| **Process/session lifecycle** | `mngr create --template chat`; `mngr start --restart --no-resume`; resume injects a synthetic turn; `claude_process_started` marker mtime = process boundary | Moderate–Deep |
| **Auth** | `claude auth status --json` keys; `claude auth login --claudeai/--console` driven via pexpect against OAuth-URL regexes; `.claude.json` `customApiKeyResponses.approved` keyed by **last 20 chars** of the key; onboarding-dialog dismissal; `mngr list type==claude` | **Deep (deepest; no seam at all)** |
| **Auth-error detection** | curated Claude/Anthropic error strings (`Please run /login`, `Invalid API key`, `authentication_error`, `401`, …) | Deep |
| **Activity "is working" state** | no run-state signal from Claude → inferred purely from unmatched `tool_use` / tail-event type + UTC-Z timestamps + process marker (Claude's own `active` marker deliberately rejected as unreliable) | Deep |
| **Progress steps** | built from `tk` (`step_enrichment`) joined to transcript order; couples to `tk` (our own tool), not Claude | Shallow (re: Claude) |

**The one real abstraction seam that already exists:** the parser normalizes Claude JSONL into a harness-neutral event vocabulary (`user_message`/`assistant_message`/`tool_result`) carrying a `source: "claude/common_transcript"` field, and *everything downstream consumes those event dicts, not raw JSONL*. The `source` field was clearly designed with multiple sources in mind. **A second harness's transcript could plug in here via a new parser** — except for leaks across the seam (the `Agent` tool name, subagent file layout, literal-string classification in the frontend, auth-error patterns). **Auth and lifecycle have no seam at all.**

### 1.4 Sub-agent spawning & the `claude` CLI dependency

**There are two distinct sub-agent mechanisms, and they couple very differently.**

1. **mngr-level worker delegation** (`launch-task` / `create_worker.py`): spawns a *separate full mngr agent* in its own container/worktree, communicating via a file-based task/report protocol. This layer is harness-agnostic (zero `claude` references).
2. **Claude Code's own native in-process subagents** (the `Agent`/Task tool): **these are available and actively used** — by the main agent directly *and* by the `imbue-code-guardian` gates (`/autofix`, `/verify-conversation`, etc.), which currently spawn subagents to do their work. The `system_interface` UI has first-class handling for them: `session_watcher._discover_subagent_sessions` walks `<sid>/subagents/agent-<id>.jsonl` + `.meta.json`, and `session_parser` special-cases the `Agent` tool to render subagent cards (reading `description`/`subagent_type`). **This is a real, Claude-Code-specific coupling**, not a non-coupling — the `Agent` tool name, the subagent transcript layout, and the `toolUseId`/`meta.json` linkage are all CC internals.

What I previously mis-stated: native subagents are **not** disabled here.
- The `--disallowed-tools` list is `AskUserQuestion, ExitPlanMode, TodoWrite, TaskCreate, TaskList, TaskUpdate`. **The `Agent`/Task subagent tool is *not* on it.** `TaskCreate/TaskList/TaskUpdate` are mngr's MCP *task-tracking* tools (disallowed because this repo uses `tk` for that), and `TodoWrite` is the todo tool — none of these is subagent spawning.
- Disabling the `claude_subagent_proxy` plugin (`.mngr/settings.toml`) does **not** turn off subagents. The proxy is an *experimental mngr feature* that would re-route Claude's `Agent` tool to spawn separate mngr-managed agents (and which wedges parents in a Haiku retry loop on error). With it off, subagents simply run as **normal native Claude Code subagents**, in-process — which is exactly why the UI renders them.

| Component | Claude-specific? | Severity |
|---|---|---|
| `create_worker.py` launch/await driver (4 `mngr` commands + poll a `report.md`) | **No** — zero `claude` references | Shallow (reusable verbatim) |
| Task-file / report-file protocol, worker-skill installer | **No** | Shallow |
| **Native Claude `Agent`/Task subagents** (used by main agent + code-guardian gates; rendered by `system_interface`) | **Yes** — `Agent` tool name, `<sid>/subagents/*.jsonl` layout, `toolUseId`/`meta.json` linkage | **Deep** (another harness's subagent model needs its own discovery + rendering) |
| Skill-lifecycle workers (crystallize/heal/update) | mostly no; only the `common_transcript.sh` transcript flush is CC-specific and **self-disables on non-claude agents** | Shallow–Moderate |
| `.mngr/settings.toml` agent type/flags/model (`type="claude"`, `--dangerously-skip-permissions`, `--disallowed-tools`, `--append-system-prompt`, `model=opus[1m]`, `sleep infinity && claude`) | **Yes (hard)** — knows the `claude` binary's arg shape | Moderate (add a new `agent_types.<harness>`) |
| CC env hardening (`DISABLE_AUTOUPDATER`, `CLAUDE_CODE_*`, `ENABLE_CLAUDEAI_MCP_SERVERS`) | Yes | Shallow (remap) |
| Install/version-pin (`Dockerfile` `CLAUDE_CODE_VERSION=2.1.160`; `setup_system.sh` installs from `claude.ai/install.sh`; mngr refuses version mismatch) | Yes (hard) | Moderate |
| Auth sharing (single shared `CLAUDE_CONFIG_DIR` across all agents) | Yes (hard) | Moderate |

**Key finding:** the **mngr worker-delegation architecture is portable** ("ask mngr to create an agent, hand it a task file, poll a report file"). But the repo *also* relies on **Claude Code's native subagents** (for the agent itself and for the code-guardian quality gates), which is a genuine CC coupling that surfaces in the `system_interface` rendering layer (1.3). Porting therefore has to handle a second harness's native subagent model — discovery, transcript layout, and linkage — not just the mngr worker path.

---

## Part 2 — The harness landscape

### 2.1 The three genuine cross-harness standards (all now under the Linux Foundation's AAIF)

1. **MCP** — near-total convergence. *Every* harness surveyed (Claude Code, Codex, Gemini/Antigravity, Cursor, opencode, Amp, Goose, Aider) is an MCP client. Build one MCP server, all consume it. Divergence is cosmetic (tool-name prefixes). **pi is the lone holdout** (deliberately no native MCP).
2. **AGENTS.md** — the converging instruction-file standard. Native/primary in Codex, opencode, Amp, Antigravity; read-alongside in Cursor, Goose; selectable via config in Gemini CLI; **Claude Code is the holdout** (reads it only via `@import` from `CLAUDE.md`). pi reads either `AGENTS.md` or `CLAUDE.md`.
3. **agentskills.io `SKILL.md`** — fast-emerging (Anthropic, Dec 2025; ~32 tools by Mar 2026). Read by CC, Codex, Cursor, opencode, Gemini/Antigravity, Goose, Hermes, pi. Several read the **same `.agents/skills` path we already use**. **Aider has no skills concept.** Youngest standard — real but still settling.

### 2.2 The idiosyncratic surfaces (portability traps)

- **Hooks are bifurcated, not standardized.** A *de-facto* standard is forming around **Claude Code's contract** (JSON on stdin; exit 0 ok / exit 2 block; stdout-JSON for control). Codex implemented essentially this contract (+ extra events); Cursor advertises "Claude-Code-compatible hooks"; Gemini CLI has native hooks with a similar shape (arguably a superset — adds model-level `BeforeModel`/`AfterModel`). But **opencode breaks the model entirely** (JS/TS plugin functions, not stdin/exit-code scripts); **Goose, Amp, Aider have no CC-style hooks**; **pi** uses TypeScript extension event handlers; **Hermes** has its own dual hook system.
- **Transcript formats are the single worst portability problem.** No shared standard. Four shapes: per-session **JSONL files** (CC `~/.claude/projects/.../<sid>.jsonl`; Codex `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`; Goose; pi tree-structured `~/.pi/agent/sessions/`), **SQLite** (Cursor — undocumented, "may change"; Hermes — FTS5+WAL), **cloud-hosted** (Amp, server-side on ampcode.com), and **markdown log** (Aider). A UI parsing transcripts across harnesses needs a **separate adapter per harness**, spanning two storage media + a cloud API + a markdown log.
- **Auth diverges on a subscription-OAuth vs API-key axis** with no shared location: OS keychain (CC macOS, Goose), `~/.claude/.credentials.json` (CC Linux), `~/.codex/auth.json`, `~/.local/share/opencode/auth.json`, env vars (Aider), cloud-account-only (Amp).
- **Status line & plugin/marketplace are CC-led and only partially copied.** CC's scriptable `statusLine` is the most mature; **custom status line is an open feature request on Codex and Gemini, absent on Amp/Goose/Aider.** Plugin/marketplace systems exist in CC, Gemini (Extensions), opencode (marketplace CLI), Goose (extensions) but are *parallel*, not a shared registry.

---

## Part 3 — Decision matrices

### Matrix A — Repo coupling surface × difficulty to generalize

| Repo surface | Standard it could ride | Difficulty | Root cause |
|---|---|---|---|
| Skill *content* (`.agents/skills/`) | agentskills.io | **Trivial** | none — already portable |
| Delegation architecture (`create_worker.py`, task/report protocol) | none needed (mngr-level) | **Trivial** | none — already harness-agnostic |
| Model/effort/token config | universal concepts | **Trivial** | config spelling only |
| Hard-block hooks (commit guard, cwd guard) | CC hook contract (widely copied) | **Easy** | event-name/field renames |
| Instruction file (`CLAUDE.md`) | AGENTS.md | **Easy filename / Moderate content** | CC-specific prose (TodoWrite, Skill tool, slash cmds) |
| `mngr` agent type / install / version-pin | none (per-harness binary) | **Moderate** | new `agent_types.<harness>` + install script per harness |
| Auth sharing (shared config dir) | none | **Moderate** | per-harness credential model |
| Soft tk-step nudges (`additionalContext`, `transcript_path`) | CC hook contract (partially copied) | **Moderate–Hard** | needs non-blocking-injection + transcript-to-hook; absent on several harnesses |
| Plugin/marketplace + code-guardian gate (`/autofix`, `/verify-conversation`) | none (CC-only) | **Hard** | CC plugin system; mandatory in workflow |
| `system_interface` transcript parser | the existing `source`-tagged event seam | **Hard (per-harness adapter)** | no transcript standard; literal-string coupling |
| `system_interface` auth flow + auth-error detection | none | **Hardest (no seam)** | 100% Claude/Anthropic-specific |
| Activity "is working" inference | none (no harness exposes reliable run-state) | **Hard** | reconstructed from transcript shape |

### Matrix B — Harness × capability (vs Claude Code)

Legend: ✅ equivalent/native · 🔶 different implementation (adapter needed) · ⚠️ partial/limited · ❌ missing.

| Dimension | Claude Code | Codex CLI | Gemini CLI / Antigravity | Cursor CLI | opencode | Amp | Goose | Aider | pi | Hermes |
|---|---|---|---|---|---|---|---|---|---|---|
| Instruction file | CLAUDE.md | 🔶 AGENTS.md | 🔶 GEMINI.md/AGENTS.md | 🔶 .mdc+AGENTS.md | 🔶 AGENTS.md | 🔶 AGENT(S).md | 🔶 .goosehints+AGENTS | ⚠️ CONVENTIONS (manual) | ✅ AGENTS.md/CLAUDE.md | 🔶 SOUL/AGENTS/MEMORY |
| Skills (SKILL.md) | ✅ | ✅ (same `.agents/skills`) | ✅ (same path) | ✅ | ✅ | 🔶 toolboxes | ✅ | ❌ | ✅ | ✅ (self-improving) |
| Hooks/lifecycle | ✅ | ✅ CC-compatible | ✅ native (superset) | ✅ CC-compatible | 🔶 JS/TS plugins | ⚠️ none | ⚠️ none | ❌ | 🔶 TS extension events | 🔶 dual system |
| MCP | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ❌ native | ✅ (+ serve) |
| Sub-agents | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ native | ✅ |
| Headless/programmatic | ✅ `-p` | ✅ `exec --json` | ✅ `-p --json` | ✅ `-p` | ✅ `run`/ACP | ✅ `-x` | ✅ `run --recipe` | 🔶 `--message` | ✅ `--print` | ✅ multiple |
| Transcript persistence | JSONL (rich, documented) | 🔶 JSONL (date-tree) | 🔶 JSON checkpoints | ⚠️ SQLite (undoc) | 🔶 JSON | ❌ cloud-only | 🔶 JSONL+SQLite | ⚠️ markdown | 🔶 JSONL (tree) | ⚠️ SQLite (FTS5) |
| Auth | OAuth/API key | 🔶 ChatGPT OAuth/key | 🔶 Google OAuth/Vertex | 🔶 account | 🔶 per-provider | 🔶 cloud account | 🔶 keyring | 🔶 env keys | ✅ multi (sub+key) | 🔶 portal/multi |
| Status line | ✅ scriptable | ❌ (requested) | ❌ (requested) | ⚠️ | ⚠️ | ❌ | ⚠️ | ❌ | ✅ TUI | ✅ TUI |
| Plugins/marketplace | ✅ | ⚠️ community | ✅ Extensions | ⚠️ | ✅ | ⚠️ | ✅ | ❌ | ✅ packages | ✅ |

### Matrix C — Roadblock classification: our fault vs fundamental

| Roadblock | Self-inflicted (our org is CC-specific) | Fundamental (harnesses genuinely differ) |
|---|---|---|
| `CLAUDE.md` filename + CC-specific prose | ✅ mostly — we wrote one file for one harness | partial: each harness has its own default name |
| Plugin/marketplace dependency (`/autofix`, `/verify-conversation` mandatory) | ✅ — we made a CC-plugin a hard workflow gate | partial: no cross-harness plugin standard |
| Soft tk-step nudges need `additionalContext`+`transcript_path` | ✅ partly — we leaned on CC-only hook affordances for UX polish | ✅ partly — several harnesses lack non-blocking injection entirely |
| `system_interface` parses CC JSONL + literal strings | ✅ — we hard-coded CC internals instead of building to the `source` seam everywhere | ✅ — **no transcript standard exists**; a per-harness parser is unavoidable |
| `system_interface` auth flow | ⚠️ small — could be abstracted | ✅ **fundamental** — auth is 100% per-vendor, no standard |
| Activity "is working" inference from transcript shape | ✅ — chosen because we distrust CC's marker | ✅ — no harness exposes reliable run-state |
| mngr `type="claude"` + flags + install/pin | ✅ — single agent type hard-wired | partial: each harness is a different binary w/ different flags |
| Skills, delegation, MCP, model config | n/a — already portable | n/a |

**Reading of Matrix C:** roughly **60% of the pain is self-inflicted** (we built one-harness-deep because there was one harness) and **40% is fundamental** (transcript formats and auth have no standard and never will short of industry agreement). The self-inflicted part is *addressable by refactoring toward the seams that already exist*; the fundamental part requires *per-harness adapters no matter how clean our code is*.

### Matrix D — Per-harness "how well would it work here?" (effort to reach parity)

| Harness | Extensibility fit | Transcript/UI cost | Overall | One-line verdict |
|---|---|---|---|---|
| **Codex CLI** | ✅ excellent (hooks/skills/subagents are CC clones; reads `.agents/skills`) | 🔶 new JSONL parser (date-tree rollout) | **Best target** | Format-translation layer + one transcript adapter; status line is the only outright gap |
| **Gemini CLI / Antigravity CLI** | ✅ excellent (native hooks superset, subagents, skills at `.agents/skills`) | 🔶 new JSON-per-session parser; ⚠️ Gemini CLI being sunset June 2026 → target Antigravity CLI | **Best target (tie)** | Same as Codex; build against Antigravity CLI not Gemini CLI |
| **Goose** | ⚠️ skills+subagents+MCP yes, **no CC-style hooks** | 🔶 JSONL+SQLite parser | **Viable, degraded** | Progress-view nudges have no hook home; rest works |
| **opencode** | 🔶 skills/subagents/MCP yes, **hooks are JS/TS plugins** | 🔶 JSON parser | **Viable, rewrite hooks** | Reimplement every hook as a plugin module |
| **Cursor CLI** | ✅ CC-compatible hooks + skills + subagents | ⚠️ SQLite, undocumented, may change | **Viable; risky UI** | Transcript parsing is a moving target |
| **Hermes** | 🔶 has everything, all bespoke (dual hooks, SQLite) | ⚠️ SQLite FTS5 | **Capable but high-effort** | Powerful, but nothing maps 1:1 |
| **pi** | ❌ no native MCP, no native subagents, extension-based hooks | 🔶 JSONL (tree) | **Poor fit** | Would fight its minimalist philosophy |
| **Aider** | ❌ no skills, no hooks, no subagents | ⚠️ markdown log | **Worst fit** | Missing the core extensibility surfaces we rely on |

---

## Part 4 — Verdict & paths forward

### The core question, answered

**Are the roadblocks because our organization is Claude-specific, or because other harnesses are fundamentally different?** — **Both, in roughly a 60/40 split, and the two halves need different responses.**

- **The self-inflicted 60%** is concentrated in: the single hard-wired `type="claude"` agent type, the literal `CLAUDE.md` with CC-only prose, the mandatory code-guardian plugin gate, and the `system_interface` parser hard-coding CC internals (`Agent` tool name, literal control strings) instead of routing everything through the `source`-tagged event seam it already has. **This is refactorable.** None of it requires a harness to grow new features; it requires us to stop assuming one harness.

- **The fundamental 40%** is: **transcript persistence** (no standard — JSONL vs SQLite vs cloud vs markdown) and **auth** (100% per-vendor). For these, *no amount of cleaning up our code removes the need for a per-harness adapter.* The best we can do is define the adapter interface cleanly (the `source` event seam is the right start) and accept N implementations. Worse, some harnesses are *structurally hostile* to the `system_interface` model: **Amp stores transcripts only in the cloud** (no local file to watch), and **Cursor's SQLite is explicitly undocumented and unstable.**

- **A third category — capability gaps — caps how well a given harness can ever do.** A scriptable status line is missing on Codex/Gemini/Amp/Goose/Aider; non-blocking `additionalContext` injection (our soft tk-step nudges) is missing on several; pi/Aider lack subagents/MCP/skills outright. For these, parity is *impossible*, not just expensive — the feature would have to be dropped or reimplemented on a different surface.

### Recommended sequencing (if pursuing this)

1. **Pick the abstraction boundary first.** Formalize the `source`-tagged common-transcript event vocabulary (`session_parser.py` / `Response.ts`) into a real `TranscriptAdapter` interface, and pull the leaks (the `Agent` tool name, literal-string classification, subagent layout, auth-error patterns) behind it. This is the highest-leverage refactor — it converts "deep coupling" into "one adapter per harness."
2. **Define `HarnessProfile` config.** Replace the single `agent_types.claude` + scattered `CLAUDE_CODE_*` env + install-pin with a per-harness profile (binary, install script, version, CLI flags, model string, instruction-file name, env hardening, transcript-adapter id, auth-adapter id). The delegation architecture (`create_worker.py`) already doesn't care which harness it is — only mngr config does.
3. **Dual-write instruction files.** Generate `AGENTS.md` from a harness-neutral core, with a thin CC-specific (TodoWrite/Skill-tool/slash-command) appendix only where needed. Symlink `CLAUDE.md → AGENTS.md` for CC.
4. **Target Codex and Antigravity CLI first** (Matrix D). They validate the abstraction with the least friction (skills are nearly free; hooks port with renames). Prove the two adapters (transcript + auth) against them before touching the harder harnesses.
5. **Decide the policy for capability gaps** (status line, soft nudges): either degrade gracefully (feature-detect and skip) or reimplement on the nearest available surface (e.g. a `Stop`/`PostToolUse` hook printing to a pane in lieu of a status line). Make this an explicit per-`HarnessProfile` capability flag, not an assumption.
6. **Treat the auth flow as per-harness from the start** — there is no seam to share; design `AuthAdapter` alongside `TranscriptAdapter`.

### What to *not* bother abstracting

- Skills, the delegation/report protocol, model/effort config, MCP usage — already portable; leave them.
- Amp and Aider — Amp's cloud-only transcripts and Aider's missing extensibility surfaces make them poor returns on effort; exclude unless a specific user needs them.

---

## Appendix — confidence notes

- In-repo coupling findings are first-hand from the source (file:line cited in the per-area sub-reports this document synthesizes).
- External harness facts are from official docs cross-checked against dated community sources; the fastest-moving areas (Codex subagents/plugins, Antigravity-vs-Gemini-CLI convergence, Cursor's SQLite layout) should be re-verified before implementation — they were changing as of early-mid 2026.
- "pi" = Mario Zechner / Earendil's terminal agent (not Imbue's, not Inflection's Pi). "Hermes" = Nous Research's `hermes-agent` harness (distinct from the Hermes *model* family). Neither is an Imbue product.
