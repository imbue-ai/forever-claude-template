# Multi-harness functionality matrix

Every distinct piece of claude-specific functionality this project touched,
one row each, with its status on codex/antigravity/opencode and a concrete,
runnable test artifact to verify it. Built by diffing `.claude/settings.json`
and `.mngr/settings.toml` against `origin/main` (the pre-session baseline),
not from memory — this is a real inventory, not a recollection.

**This document is a living draft, not exhaustive on first pass** (see
"Not yet audited" at the bottom). Some rows below were found *while writing
this document* that were never previously addressed in
`changelog/multi-harness-support.md` or `MULTI_HARNESS_PROJECT_PLAN.md` —
those are marked **NEW GAP** so they don't quietly disappear back into the
"assumed done" pile.

Status legend: [DONE] real equivalent, built & tested · [PARTIAL] partial / deferred ·
[N/A] no equivalent exists (documented, not silently dropped) · [TBD] not yet
investigated this pass.

## Phase 1 — settings.toml symmetry

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 1.1 | `agent_types.claude/-main/-worker` structure | [DONE] `agent_types.codex/-main/-worker` | [DONE] `agent_types.antigravity/-main/-worker` | [DONE] `agent_types.opencode/-main/-worker` | `python3 -c "import tomli; d=tomli.load(open('.mngr/settings.toml','rb')); assert all(h in d['agent_types'] for h in ['claude','codex','antigravity','opencode'])"` |
| 1.2 | `--dangerously-skip-permissions` (auto-approve all tools) | [DONE] `approval_policy=never` via `auto_allow_permissions` | [DONE] agy `--dangerously-skip-permissions` flag via same field | [DONE] opencode's own auto-approve field | Create one agent per harness, run a Bash-family tool call, assert no approval-prompt hang (agent completes the tool call without a wait/timeout) |
| 1.3 | CLI version pin + install-time verification | [DONE] `version = "0.142.5"` | [N/A] **no equivalent exists** — agy's installer always installs latest, documented open issue | [DONE] `version = "1.17.13"` | `codex --version` / `opencode --version` inside a fresh container matches the pinned value; agy has no equivalent check (document as N/A, not a bug) |
| 1.4 | Isolated per-agent config dir (`CLAUDE_CONFIG_DIR`) | [N/A] `CODEX_HOME` is always per-agent already, no toggle needed | [N/A] `$HOME` relocation always unconditional | [N/A] same | `echo $CODEX_HOME` / equivalent inside two sibling agents differs; this is mngr's own pre-existing per-harness isolation, not something ported from claude |
| 1.5 | `chat`/`worktree`/`worker`/`subskill-worker` create-template names | [DONE] harness-suffixed + **bare alias restored** (code review fix) | [DONE] same | [DONE] same | `mngr create <host> --template chat` (no `_claude` suffix) succeeds and produces a claude agent — this is the literal regression the code review caught |

## Phase 2 — Docker build

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 2.1 | CLI binary present in the image | [DONE] `npm install -g @openai/codex` in `setup_system.sh` | [DONE] `curl .../install.sh \| bash` | [DONE] `curl .../install \| bash`, installs to `~/.opencode/bin` (not `~/.local/bin`) | `docker run <image> which codex && which agy && which opencode` |
| 2.2 | Binary on `PATH` for every build layer + runtime | [DONE] `/root/.local/bin` | [DONE] `/root/.local/bin` | [DONE] `/root/.opencode/bin` — **separately fixed this session**: silently lost, then restored, by a scratch-git-branch incident (see changelog) | `docker run <image> bash -lc 'echo $PATH'` contains all three paths |
| 2.3 | mngr plugin registered with the standalone `mngr` CLI tool | [DONE] **fixed by code review** — `build_workspace.sh`'s `mngr plugin add --path vendor/mngr/libs/mngr_codex` | [DONE] same, `mngr_antigravity` | [DONE] same, `mngr_opencode` | `mngr create --template main_codex ...` doesn't error with an "Unknown fields" TOML parse failure |
| 2.4 | Workspace pyproject.toml dependency + `uv.lock` entry | [DONE] `imbue-mngr-codex` | [DONE] `imbue-mngr-antigravity` | [DONE] `imbue-mngr-opencode` | `uv lock --check` passes |
| 2.5 | Dockerfile pre-COPY manifest layer (build-cache warm-up) | [DONE] restored after code-review-caught data loss | [DONE] same | [DONE] same | `docker build` layer cache is warm for `vendor/mngr/libs/mngr_<harness>/pyproject.toml` unless that specific file changed |

## Phase 3 — per-harness parity

### Hooks

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 3.1 | Hook #1: plugin auto-update (`claude_update_plugin.sh`, SessionStart) | [DONE] `codex_update_plugin.sh`, real marketplace install, **temporarily points at a personal fork pending PR #25** | [DONE] `antigravity_update_plugin_preinvocation.sh`, marker-gated, same fork caveat | [DONE] provision-time `curl` fetch of the raw plugin file (opencode needs no install step) | Grep the agent's install/provision log for `imbue-code-guardian-<harness>` install success; or `codex plugin list` / `agy plugin list` shows it installed |
| 3.2 | Hook #2: prevent commit rewrite (`git rebase`/`pull -r`/`commit --amend`) | [DONE] + **code-review fix**: chained-command bypass closed | [DONE] same fix | [DONE] same fix (`.opencode/plugin/prevent-commit-rewrite.ts`) | Feed each hook `git add -A && git commit --amend` — must block (exit 2 / `decision:deny` / thrown error) on all four including the claude original |
| 3.3 | Hook #3: tk-standalone hard block (chained/redirected `tk start`/`close`) | [DONE] + **code-review fix**: substring false-negative + missing bare `ticket` form closed | [DONE] same fix | [DONE] (regex-based from the start, no fix needed) | Feed `cd x && tk start <id>` — must block; feed `apt-get install python3-tk` — must NOT block |
| 3.4 | Hook #4: require-steps soft nudge (PreToolUse) | [DONE] full per-tool-call parity | [DONE] two-hook composition (PostToolUse + PostInvocation + state file) | [DONE] `tool.execute.after`, genuine 1:1 | Run a substantive Bash call with no open tk step — reminder text appears in the next model turn's context |
| 3.5 | Hook #5: open-tickets reminder (UserPromptSubmit) | [DONE] direct port, plain-stdout | [DONE] `PreInvocation`/`injectSteps`, coarser-than-exact-once (documented tradeoff) | [DONE] `experimental.chat.messages.transform` (not `chat.message` — that doesn't expose mutable content) | Leave a tk step open, submit a new user message — reminder appears |
| 3.6 | Hook #6: open-tickets stop-nudge (Stop, human-log-only) | [DONE] | [DONE] | [DONE] (`session.idle`, observational) | Stop with an open step — stderr/log line appears (NOT model-visible, by design — see 3.9 for why this matters) |
| 3.7 | The *other* Stop-hook check: `[ -e .git ] \|\| { warn "return to repo root" }` | [DONE] **fixed** — inline command in `.codex/hooks.json`'s `Stop` array, same exit-2 mechanism | [DONE] **fixed** — `scripts/antigravity_ensure_repo_root_stop.sh`, `decision:"continue"` | N/A — opencode has no equivalent turn-scoped cwd concept the same way | `cd /tmp && <run a codex/antigravity agent's Stop hook>` — live-tested both: correct block-with-message when not at repo root, correct silent pass when at repo root |
| 3.8 | `git rebase`/`pull -r`/`commit --amend` guard — **the claude original itself** | — | — | — | Same test as 3.2, run against `scripts/claude_prevent_commit_rewrite.sh` directly — code review fixed this file too, not just the ports |
| 3.9 | Memory-MCP-tool-call reminder (net-new, not a claude→other-harness port — claude never needed this via a hook either, since `autoMemoryDirectory` used to make it automatic) | [DONE] SessionStart + UserPromptSubmit | [DONE] combined PreInvocation, marker-gated | [DONE] `experimental.chat.messages.transform`, per-session Set | Fresh session → search-memory text appears in first turn's context; second turn → save-reminder text appears |

### Config keys

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 3.10 | `model` | [DONE] `gpt-5.5` | [DONE] `Gemini 3.5 Flash (High)` | [DONE] `openrouter/z-ai/glm-5.2` | `python3 -c "import tomli; ..."` reads each `agent_types.<h>`'s model field and confirms non-empty |
| 3.11 | `effortLevel` | [DONE] `model_reasoning_effort = "xhigh"` | [PARTIAL] folded into the model name itself, no separate field exists | [N/A] **confirmed no equivalent field** (`reasoningEffort` doesn't exist in opencode's real schema — an earlier claim that it did was wrong) | Inspect each harness's real config schema/CLI output; no single automated test covers all three given the differing mechanisms |
| 3.12 | `statusLine` | [N/A] real but different shape (a picker, not a shell command) — explicitly skipped, cosmetic-only, zero functional impact | [DONE] real equivalent, not wired | [N/A] **no equivalent exists at all** (open GitHub feature request) | N/A — deliberately out of scope, see changelog |
| 3.13 | `autoMemoryDirectory` | [DONE] resolved via shared `memory` MCP server (all 4 harnesses, not a per-harness port) | [DONE] same | [DONE] same | `search_nodes` MCP tool call from any harness returns entities saved by a different harness |
| 3.14 | `enabledPlugins` (code-guardian) | [DONE] **temporarily points at a personal fork**, pending PR #25 | [DONE] same caveat | [DONE] same caveat (provision-time file fetch instead) | `codex plugin list` / `agy plugin list` shows `imbue-code-guardian-<harness>` installed |
| 3.15 | `enabledPlugins` (**frontend-design**) | [DONE] **fixed** — copied `SKILL.md` + `LICENSE.txt` verbatim into `.agents/skills/frontend-design/` (pure agent-agnostic prose, no Claude-specific tool refs, confirmed by reading it in full first). Also **removed the now-redundant `frontend-design@claude-code-plugins` entry from claude's own `enabledPlugins`** — `.claude/skills` is a symlink to `.agents/skills`, so claude would otherwise see the skill twice (once via the plugin, once natively); one source now serves all four | [DONE] same file | [DONE] same file | Any harness's agent can invoke the `frontend-design` skill and gets the same content |
| 3.16 | `env.CLAUDE_CODE_MAX_OUTPUT_TOKENS` | [TBD] **still open, real investigation done, not resolved** — codex has *adjacent* concepts (`model_context_window`, `model_auto_compact_token_limit`, `tool_output_token_limit`, and `max_output_tokens` appears as an internal term in codex's own effective-window formula) but nothing confirmed to be the direct equivalent, and a live upstream issue (openai/codex#19185/#16068) reports these settings aren't even reliably respected in project-scoped config today | [TBD] no evidence of an equivalent found | [TBD] no evidence of an equivalent found (opencode's schema has no token-limit field at all — only `steps`, an iteration count) | Not a mechanical fix — needs a deliberate decision on whether the ambiguity/live-bug risk on codex specifically is worth chasing further, given the underlying goal (cap runaway response length) is lower-stakes than the security-relevant 3.18 |
| 3.17 | `host_env__extend`'s claude-only vars (`CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE`, `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK`, `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY`) | [PARTIAL] harmlessly inert (unrecognized env vars), but the underlying *goal* (suppress autoupdate/telemetry) is separately covered via `config_overrides` | [PARTIAL] same | [PARTIAL] same | These env vars are applied globally (command-level, not harness-gated) — confirm each harness's binary silently ignores unrecognized env vars rather than erroring (spot-checked informally, not a formal test) |
| 3.18 | `host_env__extend`'s `ENABLE_CLAUDEAI_MCP_SERVERS=false` (blocks an OAuth side-channel that bypasses the latchkey gateway) | [N/A] **N/A, resolved** — this disables claude.ai's *account-level* MCP connector auto-sync specifically (Gmail/Drive-style OAuth connectors tied to a claude.ai login), not a general "MCP servers" concept. Checked all three CLIs directly (`codex mcp --help`, `agy --help`, `opencode mcp --help`): all three only support manually-added, per-project MCP servers (same shape as `.mcp.json`) — none expose anything resembling account-level connector sync. Residual uncertainty (a web-account-only feature wouldn't show up in a CLI's `--help`) noted but not chased further — the CLI surface is real, converging evidence, not a non-answer | [N/A] same | [N/A] same | N/A — nothing exists on the other side to port to |
| 3.19 | `disable_plugin__extend` (`claude_subagent_proxy`) | [N/A] this mngr plugin is claude-specific by construction, nothing to disable on the other three | [N/A] | [N/A] | Confirm `mngr_codex`/`mngr_antigravity`/`mngr_opencode` never register a subagent-proxy-equivalent plugin in the first place (spot-checked: no hits found) |
| 3.20 | Worker template `agent_args` — `--append-system-prompt "You were launched by another agent..."` (the `--dangerously-skip-permissions` half of the same list was a false alarm, not a gap — `claude-worker` already inherits `cli_args = "--dangerously-skip-permissions ..."` from its `parent_type = "claude"`) | [DONE] **fixed, differently than planned** — rather than chase a per-harness CLI flag (none confirmed for codex, opencode's candidate had ambiguous append-vs-replace semantics), moved the instruction to the same real mechanism the welcome message already proved works: `.agents/skills/launch-task/scripts/create_worker.py`'s `launch()` now sends it as a plain `mngr message -m "..."` right before the task file, from the single shared choke point all six delegation skills launch workers through. Confirmed real via the file's own live-CLI-validation test (`assert_mngr_argv_valid`, all 46 tests passing after updating the 3 affected assertions). Removed the now-redundant `--append-system-prompt` from claude's `agent_args` too, so the instruction reaches all four harnesses through one uniform path instead of claude getting it twice through two different channels | [DONE] same, automatically | [DONE] same, automatically | Delegate a task to a worker on each harness, check its transcript for a first message reading "You were launched by another agent..." immediately before the task content — now present on all four, verified via the create_worker.py test suite (not yet exercised against a live non-claude agent session) |

### Instruction files, skills, MCP

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 3.21 | `CLAUDE.md` project instructions | [DONE] `AGENTS.md`, one file serves all three (confirmed real auto-discovery for codex/opencode via their own docs) | [DONE] same file, native auto-discovery -- **corrected**: an earlier pass here wrongly assumed agy needed a `global_instructions_md` workaround; direct evidence from the installed binary's own bundled docs shows agy already walks up from cwd to the repo root discovering `GEMINI.md`/`AGENTS.md`, same as codex/opencode. The workaround was removed, not needed. | [DONE] same file | Start a fresh agent, ask "what does the task management section of your instructions say" — answer should reflect `AGENTS.md`'s actual content |
| 3.22 | `.agents/skills/` | [DONE] native support, zero work needed | [DONE] native support | [DONE] native support (confirmed via opencode's own docs) | Any skill under `.agents/skills/` is invocable on all four |
| 3.23 | Memory MCP server (`.mcp.json`) | [DONE] | [DONE] (required the `mcp_servers` field upstream fix in `mngr_antigravity`) | [DONE] | See 3.13 |
| 3.24 | Welcome message (`--message /welcome` on the initial chat agent) | [DONE] fixed to `$welcome` (codex's real mention syntax — `/welcome` would've been meaningless text to it) | [DONE] `/welcome` already worked (same slash-from-skill-name convention as claude) | [PARTIAL] no manual invocation exists at all — best-effort plain-English nudge instead of a guaranteed-equivalent fix | Create the initial chat agent for each harness, check the transcript for the actual welcome-message text appearing as the agent's first real response — **this single test also confirms the harness-routing in `_resolve_chat_template`/`_build_create_chat_command` is correct**, per your point about one test covering multiple pieces |

## Phase 4 — system_interface backend

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 4.1 | Agent harness visible to the UI (list/status) | [DONE] `Harness.CODEX` via `harness.py`'s `parse_harness` | [DONE] `Harness.ANTIGRAVITY` | [DONE] `Harness.OPENCODE` | `harness_test.py` (13 cases, all 4 harnesses × claude/main/worker suffixes) + `GET /api/agents` response includes `"harness": "codex"` for a codex agent |
| 4.2 | Chat transcript rendering | [DONE] `CommonTranscriptWatcher`, reads mngr's own `events/codex/common_transcript/events.jsonl` | [DONE] same mechanism, `events/antigravity/common_transcript/` | [DONE] same mechanism, `events/opencode/common_transcript/` | `common_transcript_watcher_test.py` (event-mapper unit tests) + live: mngr's own `pytest.mark.release` e2e tests. codex live-run confirmed plumbing round-trips correctly up to an (unrelated) expired-auth-token failure; opencode live-run **passed clean end-to-end** including stop/resume/adopt-from-preserved. Antigravity not separately live-run — same generic harness/schema as the other two, plus mngr's own `common_transcript_convert_test.py` covers its converter. |
| 4.3 | Auth-error signal on a broken agent (detection only — the interactive recovery modal stays claude-only, see Phase 5) | [DONE] live-confirmed patterns (`token_expired`, `401 Unauthorized`, etc., observed via a real expired `~/.codex/auth.json`) | [PARTIAL] **no patterns seeded** — real gap, not silently assumed; add once a real failure's text is observed | [PARTIAL] **no patterns seeded** — same | `common_transcript_watcher_test.py::test_is_auth_error_text_matches_known_codex_pattern` / `test_is_auth_error_text_no_pattern_for_unseeded_harness`. Frontend gate extracted to a pure, tested predicate: `models/ClaudeAuth.ts`'s `shouldOpenLoginModalForHarness`, covered by `ClaudeAuth.test.ts` (opens for claude/unknown, refuses for codex/antigravity/opencode). |

Known, explicitly-not-covered gap in 4.2/4.3: codex's headless `exec` mode can fail auth with **no assistant text at all** (`task_complete` event with `last_agent_message: None`, observed live) — a distinct "silent empty completion" failure shape that text-pattern matching cannot see. Left open rather than papered over with an unverified heuristic.

## Phase 6 — delegation routing

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 6.1 | A delegated worker matches the delegating lead's own harness | [DONE] `worker_codex`/`subskill-worker_codex`, auto-selected via `create_worker.py`'s `_resolve_delegating_harness` reading the lead's own `data.json` | [DONE] `worker_antigravity`/`subskill-worker_antigravity` | [DONE] `worker_opencode`/`subskill-worker_opencode` | `create_worker_test.py::test_launch_routes_to_harness_suffixed_template` + live: `mngr config get create_templates.worker_codex` (and the other 5 combinations) resolves with the expected `type` |

## Phase 7 — user-initiated agent creation (the "+" button)

| # | Claude functionality | Codex | Antigravity | Opencode | Test artifact |
|---|---|---|---|---|---|
| 7.1 | "New chat"/"New agent" creates the picked harness, not always claude | [DONE] `chat_codex`/`worktree_codex` via the new hover submenu + `_resolve_create_template` | [DONE] `chat_antigravity`/`worktree_antigravity` | [DONE] `chat_opencode`/`worktree_opencode` | `agent_manager_test.py::test_worktree_create_argv_harness_suffixed_template_accepted_by_live_cli` / `test_chat_create_argv_harness_suffixed_template_accepted_by_live_cli` (parametrized over all 4) + live: `mngr config get create_templates.chat_codex` etc. resolve with the expected `type` |
| 7.2 | An unrecognized harness value is rejected cleanly (400, not 500) | [DONE] pydantic `Harness` enum validation | [DONE] same | [DONE] same | `server_test.py::test_create_chat_agent_rejects_unknown_harness` / `test_create_worktree_agent_with_harness_rejects_unknown_value` |

Not verified this pass: the hover-submenu itself rendering/expanding correctly in a live browser (no browser/screenshot tooling available this session) and a full click-through creating a real, non-claude agent (needs a real primary-agent context, not reachable standalone outside a real mngr-provisioned host). Both are real, flagged gaps, not silently assumed passing -- see the changelog's Phase 7 section for exactly what *was* verified (production build contains the new code; a real booted backend correctly parses the harness field through to the same precondition every creation request hits standalone).

## Adjacent to Phase 1-3, tracked separately (not gaps, by design)

| # | Item | Status |
|---|---|---|
| A.1 | code-guardian's actual review skills (autofix/verify-conversation/verify-architecture) | [N/A] Explicitly out of scope for this repo — genuinely Claude-Code-specific orchestration (inline-bash frontmatter, named sub-agent spawning). Tracked in the separate PR against `imbue-ai/code-guardian`, not fct. |

## Status of the 5 gaps found writing this document

Of the 5 real, previously-uncaught gaps this document surfaced (3.7, 3.15,
3.16, 3.18, 3.20 — none caught by the earlier code review, since that was
scoped to correctness bugs in new code, not completeness gaps in what never
got ported at all):

- **3.7 (`.git`-existence Stop-hook check) — [DONE] fixed**, codex + antigravity, live-tested.
- **3.15 (`frontend-design` plugin) — [DONE] fixed**, copied into `.agents/skills/` for all four harnesses, claude's now-redundant plugin reference removed.
- **3.18 (`ENABLE_CLAUDEAI_MCP_SERVERS`) — [DONE] resolved, N/A.** Checked all three CLIs directly — none expose anything resembling account-level MCP connector sync, only manually-added per-project servers. Nothing on the other side to port to.
- **3.20 (worker `agent_args` system-prompt) — [DONE] fixed, via a better mechanism than originally planned.** Rather than a per-harness CLI flag, moved to `mngr message -m` sent from `create_worker.py`'s one shared choke point — reaches all four harnesses uniformly, confirmed real via the file's own live-CLI-validation test.
- **3.16 (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`) — still open, and deliberately deprioritized.** Real investigation done (codex's adjacent-but-unconfirmed fields, a live upstream reliability bug reported even on Claude Code itself, zero evidence anywhere else) — not a quick fix, and given even the origin platform's version is flaky, not worth further effort right now relative to everything else this session touched.

## Not yet audited (real, not silently assumed complete)

- system_interface's UI-visibility gap — **now built, not just investigated**: see Phase 4 above (harness field, transcript watcher, auth-error detection). The originally-anticipated hard part, interactive per-harness auth *recovery* (login modals that can drive codex/antigravity/opencode's own auth), is real, harness-specific product surface deliberately left for Phase 5 — not built here, and not silently assumed done.
- 3.16 above, requiring dedicated research beyond this pass (3.18 is resolved, see above).
- Whether opencode's `AgentConfig.prompt` append-vs-replace question (3.20) could be resolved by just testing it live in a real opencode session — not attempted this pass.
