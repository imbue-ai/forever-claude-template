- Added **codex**, **antigravity**, and **opencode** as full peers of claude in
  `.mngr/settings.toml`. Every claude-only block was made a 4-way parallel
  chain, with claude given no naming privilege:
  - `[agent_types.<harness>]` (base, shut-up + skip-permissions settings),
    `[agent_types.<harness>-main]` (services agent), `[agent_types.<harness>-worker]`
    (delegation target) for all four harnesses. `[agent_types.main]` /
    `[agent_types.worker]` renamed to `[agent_types.claude-main]` /
    `[agent_types.claude-worker]` to match.
  - `[create_templates.main_<harness>]` (new -- thin overlays stacked on top
    of the now-harness-agnostic `[create_templates.main]`, each setting only
    `type` and a new `FCT_HARNESS` env var), and `[create_templates.chat|worktree|worker|subskill-worker_<harness>]`
    for all four (the claude ones renamed from their old unsuffixed names).
  - Removed the silent `type = "claude"` default in `[commands.create]` --
    every `mngr create` from this repo must now specify a type/template
    explicitly, since a silent default is a footgun with 4 harnesses instead
    of 1.
  - Fixed the `sleep infinity && claude` window-0 no-op (a claude-shaped hack
    that would have needed reinventing 3 more times) to a shared,
    harness-agnostic `bash -c 'exec sleep infinity' --`, used by all four
    `-main` types. (The trailing `--` makes mngr's `assemble_command`, which
    always appends `cli_args` after `command`, dump those args into bash's
    unused positional params -- silently discarded regardless of content.)

- Updated `libs/bootstrap/src/bootstrap/manager.py` to read the new
  `FCT_HARNESS` env var (set by whichever `main_<harness>` template created
  the workspace; defaults to `claude` with a warning if unset, for backward
  compatibility with pre-existing workspaces) and dispatch on it in the one
  function that was claude-specific, `_bootstrap_init_chat_dir`:
  - The `CLAUDE_CONFIG_DIR` host-env write now only runs for claude -- the
    other three harnesses have no shared-config-dir mechanism to write a var
    *for* (see Open Issues), so this step is skipped for them entirely,
    not "genericized."
  - `_build_create_chat_command` now picks `--template chat_<harness>`
    instead of a hardcoded `--template chat`.
  - Both changes verified with a standalone functional smoke test (all 4
    harnesses, the unset-default case, and an unrecognized-value fallback)
    since the existing pytest suite couldn't be run without modifying
    tracked dependency files as a side effect.

- Wired codex/antigravity/opencode's mngr plugins and CLI binaries into the
  actual build, so the container can run all three, not just claude:
  - `pyproject.toml`: added `imbue-mngr-codex`/`imbue-mngr-antigravity`/
    `imbue-mngr-opencode` to `[tool.uv.sources]` (mirroring
    `imbue-mngr-claude`, all pointed at their already-vendored
    `vendor/mngr/libs/mngr_<x>` paths -- the plugin source was already
    vendored, only the wiring was missing) and to `[dependency-groups].dev`
    (so `uv sync --all-packages` registers them in `/mngr/code/.venv` and
    pluggy can discover them via entry points -- without this,
    `agent_types.codex`/etc in settings.toml fail to parse as "Unknown
    fields"). `uv lock` re-resolved cleanly (367 packages, added
    `imbue-mngr-antigravity` v0.1.8, `imbue-mngr-codex` v0.1.4,
    `imbue-mngr-opencode` v0.2.17).
  - `Dockerfile`: added `CODEX_CLI_VERSION`/`OPENCODE_CLI_VERSION` build
    args (antigravity has none, per Phase 1's finding), `COPY` lines for
    the three plugins' `pyproject.toml`s in the pre-dependency-install
    layer, and `/root/.opencode/bin` on `PATH` (opencode installs there,
    not `~/.local/bin` like claude/agy).
  - `scripts/setup_system.sh`: added install steps for all three CLIs,
    pinned to the same versions as `.mngr/settings.toml`'s `version`
    fields -- codex via `npm install -g @openai/codex@<version>` (must run
    after the existing Node.js install step), antigravity via its curl
    installer (no version pin possible), opencode via its curl installer
    with `VERSION=<version>` (confirmed exact command from
    `mngr_opencode`'s own plugin source, not guessed).
  - **Verified with a real, full `docker build -t fct-multi-harness-test -f
    Dockerfile .`, exit 0, image exported successfully.** Confirmed directly
    from the build log, not inferred: `✅ Antigravity CLI installed
    successfully at /root/.local/bin/agy`; `Installing opencode version:
    1.17.13` (exact pinned version); `command -v codex` → `/usr/bin/codex`;
    all three new mngr plugins built and installed into the venv
    (`imbue-mngr-codex==0.1.4`, `imbue-mngr-antigravity==0.1.8`,
    `imbue-mngr-opencode==0.2.17`) alongside `imbue-mngr-claude==0.2.17`.

- Replaced claude's `autoMemoryDirectory` (a claude-only feature, no
  equivalent on the other three harnesses) with a **shared memory MCP
  server**, so memory is a property of the workspace, not of any one
  harness -- a fact one agent learns is visible to any other agent in the
  same workspace later, regardless of which CLI it runs. Uses the official
  local `@modelcontextprotocol/server-memory` (zero external account, no
  new credential), all agents pointed at the same
  `runtime/memory/memory.jsonl` (git-backed via the existing runtime-backup
  branch, same as before):
  - `.claude/settings.json`: removed `autoMemoryDirectory`.
  - `.mcp.json` (new): registers the `memory` server for claude, the real
    Claude Code project-level MCP config convention.
  - `.mngr/settings.toml`: added the same server to `agent_types.codex`
    (`config_overrides.mcp_servers`, codex's native format),
    `agent_types.opencode` (`config_overrides.mcp`, opencode's native
    format), and `agent_types.antigravity` (`mcp_servers`, confirmed real
    against a live agy install: `~/.gemini/config/mcp_config.json`, same
    `{"mcpServers": {...}}` shape as claude/codex). Antigravity needed a
    real upstream code change first -- `mngr_antigravity`'s
    `AntigravityAgentConfig` had no field writing to that file at all (MCP
    config lives in a file separate from `settings.json`, unlike the other
    three harnesses where it's just another section of the same file
    `config_overrides` already writes). Added `mcp_servers: dict[str, Any]`
    + `get_antigravity_mcp_config_path()` + the write in `_provision_agy_home`,
    in both `~/imbue/mngr` and fct's `vendor/mngr` (a separate physical
    copy, not a symlink -- kept in sync by copying the 4 changed files
    over, diffed identical beforehand so nothing fct-specific was
    clobbered). 2 new tests added (write-when-set, no-write-when-unset);
    full `antigravity_config_test.py` + `plugin_test.py` suite re-run,
    131 passed, 0 failed.
  - `create_templates.main`: added `mkdir -p runtime/memory` so the
    directory exists before any harness's memory-server subprocess tries
    to write to it.
  - `CLAUDE.md`: rewrote the Memory section to describe calling the
    `memory` MCP server's tools instead of "Claude's built-in memory
    system" -- this is a real system-prompt change, not just config, since
    the old instruction became false the moment the mechanism changed.
    Note: codex/antigravity/opencode don't have their own `AGENTS.md` yet
    (Phase 3's instruction-file work is still open) -- when those get
    written, they need this same memory section.
  - Verified: `.mngr/settings.toml` and `.mcp.json` both parse correctly,
    `@modelcontextprotocol/server-memory` confirmed to exist on the real
    npm registry (version 2026.7.4).

- Ported `claude_require_steps_pretool.sh` (the "no active `tk` step, soft
  reminder before substantive tool use" hook) to codex, antigravity, and
  opencode -- full per-tool-call parity for all three, not the weaker
  "fold into the turn-start reminder" compromise considered earlier (that
  conclusion was based on only checking pre-execution hooks; checking
  post-execution ones found real mechanisms for all three):
  - **codex**: `scripts/codex_require_steps_pretool.sh` + `.codex/hooks.json`
    -- direct port, same `additionalContext` mechanism as claude.
  - **opencode**: `.opencode/plugin/require-steps.ts` -- `tool.execute.after`
    gives the tool name and a mutable result-text field in one hook, genuine
    1:1 parity. Verified: valid TS (esbuild, 0 errors), and the exact Bun
    Shell API chain used live-tested with real Bun.
  - **antigravity**: `scripts/antigravity_flag_missing_step_posttooluse.sh`
    (`PostToolUse`, sees the tool name, writes a flag) +
    `scripts/antigravity_require_steps_postinvocation.sh` (`PostInvocation`,
    can inject text, reads/clears the flag) + `.agents/hooks.json` -- two
    cooperating hooks via a state file, since no single agy hook has both
    capabilities. Same file-based-signaling pattern `mngr_antigravity`
    already uses internally. **Known limitation**: only `run_command` (the
    one confirmed real agy tool name) is treated as substantive -- agy's
    full tool taxonomy is still unenumerable.
  - All bash scripts syntax-checked, all JSON configs validated. Not
    exercised against a real running agent session in any of the three.

- **REVERTED.** An earlier pass of this branch embedded a trigger +
  gate-check port of `imbue-code-guardian` directly in fct (harness-specific
  hook scripts + an opencode plugin, calling copies of the plugin's own
  `config_utils.sh`/`stop_hook_gates.sh`). That approach was discarded in
  favor of fixing this at the source: `imbue-ai/code-guardian` itself is a
  separate repo, referenced by fct only as a marketplace/plugin pointer in
  `.claude/settings.json` (`extraKnownMarketplaces`/`enabledPlugins`) --
  never vendored or embedded (unlike `vendor/mngr`, which is a real
  physical copy). Now that codex is confirmed to have a real curated
  plugin marketplace (see `MULTI_HARNESS_PROJECT_PLAN.md`'s correction),
  the right fix is publishing real codex/antigravity/opencode plugin
  variants from that repo directly, so any project picks them up via each
  harness's own idiomatic install mechanism -- not duplicating hook logic
  per downstream project. That work now lives as a PR against
  `imbue-ai/code-guardian`, not in fct. fct's own side of this stays a
  one-line config pointer, same shape as claude's today, once those
  variants exist.

<details>
<summary>What was reverted (for history)</summary>

Deleted: `scripts/reviewer_config_utils.sh`, `scripts/reviewer_gates.sh`,
`scripts/codex_reviewer_stop.sh`, `.opencode/plugin/reviewer-gate.ts`, and
the `Stop` hook entry in `.codex/hooks.json` that wired the latter in.
Original notes on what that version did, before deletion:
  - `scripts/reviewer_config_utils.sh` + `scripts/reviewer_gates.sh` --
    verbatim copies of the plugin's `config_utils.sh` /
    `stop_hook_gates.sh`. Confirmed genuinely harness-agnostic already (pure
    bash, marker-file checks, no Claude-specific paths) -- zero code
    changes needed, only a rename for the sourced-file reference.
  - `scripts/codex_reviewer_stop.sh` + `.codex/hooks.json`'s new `Stop`
    entry -- ports the `enabled_when` gate from `stop_hook_orchestrator.sh`
    (respects `.reviewer/settings.json`, disabled by default, same as
    claude's behavior today) then calls `reviewer_gates.sh` directly --
    codex's Stop hook accepts the same exit-2 + stderr-reason fallback as
    claude, confirmed via developers.openai.com/codex/hooks, so no JSON
    wrapping was needed.
  - **Real, live-tested, not just syntax-checked**: verified the disabled
    default correctly no-ops (exit 0); verified the enabled path on a
    scratch branch with a real diff and no review-output marker files
    correctly exits 2 with "The following review gates have not been
    satisfied: - architecture verification (/verify-architecture)". Scratch
    branch/commit deleted after, confirmed `main` untouched.
  - Antigravity's Stop hook confirmed to support the same forcing
    mechanism (`decision: "continue"`, from its own hooks.md, already read
    earlier) -- not yet wired, same porting pattern as codex should apply.
  - **Opencode has no native equivalent mechanism** -- confirmed by reading
    its actual typed `Hooks` interface, not assumed: no Stop/session-end
    hook exists that can block or force re-continuation. Its only related
    hook (`event`) is purely observational, no output channel. **Workaround
    implemented**: `.opencode/plugin/reviewer-gate.ts` -- on `session.idle`
    (root session only), calls `reviewer_gates.sh` directly (single source
    of truth, no reimplemented gate logic), and if unsatisfied uses the
    plugin's live SDK `client.session.promptAsync` (a generated, versioned
    API, confirmed real via the SDK's own type defs) to inject the same
    reason text back into the session -- the same mechanism mngr's own
    opencode plugin already uses to deliver messages, just called from
    inside the plugin instead of over HTTP from the host. Anti-loop
    safeguard: a flag file (`.reviewer/outputs/.opencode_last_nagged_head`)
    keyed on the current commit hash ensures each commit gets nagged at
    most once, regardless of how many times the session goes idle without
    HEAD moving -- without this, the injected prompt making the session
    busy again could re-trigger the same idle event indefinitely.
    **Verified live, not just syntax-checked**: valid TS (esbuild, 0
    errors); the exact Bun Shell chain used (`.cwd().quiet().nothrow()`,
    including the nested `bash -c` invocation for the `enabled_when`
    check) live-tested with real Bun against the real repo -- caught and
    fixed a real bug in the process (a leftover `.reviewer/settings.local.json`
    from earlier codex testing made the first test run give a false
    positive; removed, confirmed gitignored/never tracked, re-tested clean);
    the underlying `reviewer_gates.sh` call verified to correctly return
    exit 2 with the expected message on a scratch branch with an unsatisfied
    gate. Scratch branches/commits deleted after, `main` confirmed
    untouched both times.
    **Risk, stated plainly**: `promptAsync` itself is reasonably stable
    (generated SDK), but `session.idle` is one confirmed event name with no
    fallback -- mngr's own opencode plugin has already hit one similar
    event-naming inconsistency before (`permission.asked` vs
    `permission.updated`, handled by accepting both). If opencode ever
    renames `session.idle`, this silently stops firing rather than breaking
    anything -- a "quietly stops working" risk, not a corruption risk.
  - **NOT ported**: the actual `/autofix`, `/verify-conversation`,
    `/verify-architecture` skills. Read `skills/autofix/SKILL.md` in full --
    it's genuinely Claude-Code-specific in a way the gate-check isn't: uses
    Claude Code's `` !`command` `` inline-bash-in-frontmatter execution,
    spawns sub-agents by name via Claude's own Agent/Task tool, and lists
    Claude-specific tool names (`AskUserQuestion`) in `allowed-tools`. This
    is a 10-iteration fix loop with real control flow, not portable prose
    like the individual `agents/*.md` review prompts (which ARE just
    plain-English prompts + YAML frontmatter, genuinely agent-agnostic
    content) -- porting the skills properly means rewriting that
    orchestration in each harness's own subagent-spawning idiom. Until
    that's done, a blocked codex agent will be told to run slash commands
    that don't exist for it yet -- this lands trigger+detection
    infrastructure only, not a working enforcement pipeline.

</details>

- Ported the three remaining "genuinely simple" hooks (per the earlier
  feasibility table) to codex and antigravity -- opencode's version needed
  restructuring since its only blocking mechanism is `tool.execute.before`
  throwing:
  - **Hook #2** (`claude_prevent_commit_rewrite.sh`, block git
    rebase/pull --rebase/commit --amend|--fixup): `codex_prevent_commit_rewrite.sh`
    is a verbatim port (same exit-2+stderr contract, confirmed valid for
    codex's `PreToolUse` too, not just `Stop`). `antigravity_prevent_commit_rewrite.sh`
    wraps the same logic in agy's JSON `{"decision": "deny", ...}` shape,
    since no bare exit-code fallback is confirmed for its `PreToolUse`.
    Opencode's version lives in `.opencode/plugin/prevent-commit-rewrite.ts`.
  - **Hook #3** (`claude_tk_standalone.sh`, hard-block chained/redirected
    `tk`/`ticket` calls): both `codex_tk_standalone.sh` and
    `antigravity_tk_standalone.sh` shell out to the exact same
    `claude_tk_standalone_check.py` unmodified (it's pure `shlex` parsing,
    already agent-agnostic) -- only the wrapper's input-field names and
    output format differ per harness. **Caught and fixed a real bug before
    testing**: the antigravity wrapper originally captured the checker's
    exit code via `reason=$(...); exit_code=$?`, but under `set -e` a
    nonzero exit from the command substitution aborts the script *before*
    that assignment runs -- fixed to `reason=$(...) || exit_code=$?`.
    Opencode's version is folded into `prevent-commit-rewrite.ts` (both
    hooks share the one `tool.execute.before` opencode has, since it can
    only ever throw-to-block, not run two independent PreToolUse-style
    hooks the way the shell-hook harnesses can).
  - **Hook #6** (`claude_open_tickets_stop_nudge.sh`, non-blocking stop
    reminder): the simplest of the six -- always exits 0, so no
    output-format translation needed at all. `codex_open_tickets_stop_nudge.sh`
    is verbatim. `antigravity_open_tickets_stop_nudge.sh` emits `{}` (no
    `decision` field, so the stop proceeds normally) instead of relying on
    silent stdout. Opencode's version, `.opencode/plugin/open-tickets-stop-nudge.ts`,
    listens on the same `session.idle` event as the reviewer-gate workaround
    but is purely observational (`console.error`, no session interaction).
  - Wired into `.codex/hooks.json` (both new `PreToolUse` entries added to
    the existing matcher group, in the same order as claude's
    `.claude/settings.json`: prevent-commit-rewrite, tk-standalone,
    require-steps; `open_tickets_stop_nudge` before `reviewer_stop` in the
    `Stop` array so the informational nudge always gets a chance to log
    even if the reviewer gate blocks) and `.agents/hooks.json` (each as its
    own named group, matcher `"run_command"` for the two `PreToolUse`
    ports).
  - **All verified live with real hook-shaped JSON input, not just syntax
    or `bash -n`**: confirmed each of the 6 new bash scripts blocks/allows
    correctly on both a triggering and a non-triggering example (e.g.
    `git rebase main` blocked, `git status` allowed; a chained
    `cd ...; tk start` blocked, a standalone `tk start` allowed). All 4
    `.opencode/plugin/*.ts` files pass esbuild with 0 errors.

- Ported hook #5 (`claude_open_tickets_reminder.sh`, `UserPromptSubmit` --
  reminds the agent of still-open `tk` steps at the start of a new turn) to
  codex, antigravity, and opencode -- the last of the six hooks with a real
  port (hook #1, plugin auto-update, remains unbuilt -- see
  `MULTI_HARNESS_PROJECT_PLAN.md`):
  - **codex**: `scripts/codex_open_tickets_reminder.sh` + a new
    `UserPromptSubmit` entry in `.codex/hooks.json` -- verbatim port, codex
    accepts plain stdout text for this event exactly like claude
    (confirmed via developers.openai.com/codex/hooks), zero translation.
  - **antigravity**: `scripts/antigravity_open_tickets_reminder_preinvocation.sh`
    + a new `PreInvocation` entry in `.agents/hooks.json` -- simpler than
    hook #4's antigravity port (one hook, not two-hook-plus-state-file),
    since `PreInvocation` alone has both "runs before the model" and "can
    inject text." Real bug caught before shipping: an apostrophe inside a
    heredoc wrapped in `$(...)` broke bash's parser (`bash -n` caught it
    immediately) -- isolated with a minimal repro, fixed by rewording
    rather than changing quoting style.
  - **opencode**: `.opencode/plugin/open-tickets-reminder.ts` -- turned out
    less simple than the earlier "clean 1:1 match" claim for this hook. The
    first candidate checked, `chat.message`, only exposes `message:
    UserMessage` with no `parts` array -- can't actually inject text. The
    real candidate, `experimental.chat.messages.transform`, does expose a
    mutable `parts` array per message, but two real caveats are stated
    plainly rather than assumed away: it's explicitly under opencode's
    "experimental" namespace (real API-shape-change risk), and its exact
    firing frequency relative to one user turn is unconfirmed (plausibly
    once per model call within a turn, not once per submission like
    claude's hook) -- guarded with an in-memory per-message-id dedup rather
    than assuming exactly-once semantics.
  - All bash scripts syntax-checked and live-tested with real hook-shaped
    input and a real open `tk` step (confirmed correct output shape for
    all three: plain text for codex, `{injectSteps: [...]}` for
    antigravity). The opencode plugin passes esbuild with 0 errors but was
    not exercised against a live opencode session.

- Added `AGENTS.md` at repo root -- serves codex, antigravity, and opencode
  simultaneously (confirmed all three read this same filename; one file
  turned out to be enough, not three). Built by copying `CLAUDE.md`
  (~95% of its content was already harness-neutral prose -- file-reading
  conventions, testing/ratchets, git conventions, delegation, services)
  and adjusting only the genuinely Claude-specific parts: the
  disabled-built-in-todo-tool line now states accurately that enforcement
  differs per harness (opencode's `todowrite` is disabled in config;
  codex's `update_plan` has no config-level disable, so it's a stated
  convention, not an enforced one -- no fabricated uniform claim), the
  self-modification section's self-reference, and the `.claude/skills/`
  symlink aside (dropped -- Claude-implementation detail, not relevant to
  the other three, which already read `.agents/skills/` natively).
  `CLAUDE.md` and `AGENTS.md` now cross-reference each other in their
  self-modification sections so a shared-content change gets made in both,
  not silently drift apart.

- **Correction, caught by direct user challenge, not caught proactively**:
  the claim that `AGENTS.md` "just works" for all three non-claude harnesses
  was wrong for antigravity. Verified fresh (not re-asserted from earlier
  in this doc): codex and opencode both genuinely auto-discover `AGENTS.md`
  with zero config, confirmed via their own docs. Antigravity does not --
  it natively reads only a single global `$HOME/.gemini/GEMINI.md`, no
  per-project convention at all. Fixed for real, not worked around:
  - `mngr_antigravity/antigravity_config.py`: added
    `get_antigravity_global_instructions_path()` (`<home>/.gemini/GEMINI.md`).
  - `mngr_antigravity/plugin.py`: added `global_instructions_md: str | None`
    to `AntigravityAgentConfig` + the write in `_provision_agy_home` --
    same pattern as the earlier `mcp_servers` fix (a field writing a path
    nothing wrote to before).
  - `.mngr/settings.toml`: seeds antigravity's `GEMINI.md` with a rule
    telling it to also check the project workspace for `AGENTS.md` -- the
    documented real-world workaround (confirmed via a real source, not
    invented), not a guess.
  - **SUPERSEDED, see "Correction: antigravity's `global_instructions_md`
    was never needed" further down.** This whole fix was itself still
    wrong -- "confirmed via a real source" above did not mean the installed
    binary; agy does have native per-project `AGENTS.md` discovery, and the
    entire mechanism described here was removed.
  - 2 new tests (write-when-set, no-write-when-unset); full
    `antigravity_config_test.py` + `plugin_test.py` suite re-run: 133
    passed (131 + 2). Synced into `vendor/mngr` after confirming the diff
    was exactly this change.

- **Hook #1 (plugin auto-update) implemented for all three, and
  `enabledPlugins` unblocked** -- both were previously deferred as "blocked
  on the code-guardian PR merging." Directly challenged on that assumption
  (does the plugin *have* to come from the merged repo, or can the
  reference point at the open PR itself?) -- checked hands-on with the
  real CLIs rather than continuing to assume, and all three genuinely work
  pre-merge:
  - **codex**: real syntax confirmed via docs (`codex plugin marketplace
    add owner/repo --ref branch`), then actually run live against
    `minhtrinh-imbue/code-guardian#add-codex-opencode-antigravity-support`
    -- succeeded, and the plugin didn't show up in `codex plugin list` on
    the first attempt. **Real bug found and fixed**: the marketplace
    catalog's plugin entries used `"source": {"path": "..."}`  (a nested
    object); codex silently ignored it. Fixed to a flat string
    (`"source": "./plugins/..."`, matching the claude marketplace's
    already-working convention), pushed, marketplace refreshed
    (`codex plugin marketplace upgrade`), re-verified: plugin now listed
    and `codex plugin add imbue-code-guardian-codex@imbue-code-guardian`
    installs cleanly. Also confirmed both `marketplace add` and `plugin
    add` are genuinely idempotent (exit 0 whether already present or not)
    -- `scripts/codex_update_plugin.sh` (new `SessionStart` hook) just
    re-runs both unconditionally every session.
  - **antigravity**: `agy plugin install <github-url>` works directly on a
    plain git URL, no marketplace/link step needed at all -- confirmed
    live. Pointing it at the repo root auto-triggered agy's own
    claude-plugin import feature and pulled in the *original* claude
    plugin instead of the hand-built antigravity variant; using a
    `.../tree/<branch>/plugins/imbue-code-guardian-antigravity` URL
    targets the specific plugin by path and works correctly (verified:
    correct name, hooks processed). Also confirmed idempotent.
    `scripts/antigravity_update_plugin_preinvocation.sh` (new
    `PreInvocation` hook) runs it once per agent lifetime, gated by a
    marker file rather than an unconfirmed `invocationNum` hook-input
    field (never independently verified this session) -- avoids paying a
    live network fetch on every model call within a turn.
  - **opencode**: real gap confirmed, not routed around blindly --
    `bun add github:owner/repo` genuinely works (live-tested against
    `github:lodash/lodash`), but bun does not support installing from a
    *subdirectory* of a git repo (open, unresolved `oven-sh/bun` issue
    `#15506`) -- and this plugin lives nested under `plugins/imbue-code-guardian-opencode/`,
    not at repo root. The `gitpkg.now.sh` subdirectory-tarball proxy
    commonly used for this is dead (its Vercel deployment returns 402).
    Sidestepped entirely: opencode needs no "install" ceremony at all, it
    just loads whatever `.ts` sits in `.opencode/plugin/`, so a new
    `extra_provision_command__extend` entry on `.mngr/settings.toml`'s
    opencode agent type fetches the file directly from GitHub's own raw
    CDN at provision time (`curl -fsSL raw.githubusercontent.com/.../index.ts`)
    -- confirmed live: valid TypeScript, esbuild 0 errors.
  - **All three currently point at a personal fork + PR branch**
    (https://github.com/imbue-ai/code-guardian/pull/25), explicitly and
    repeatedly flagged in each file's own header comment as temporary --
    switch to the canonical `imbue-ai/code-guardian` repo (dropping
    `--ref`/the branch path) once that PR merges. This is a real,
    deliberate tradeoff (a shared template depending on an individual's
    unreviewed fork), not an oversight.

- **Fixed the initial chat agent's welcome message for codex and opencode**
  -- a real, previously-flagged-but-never-resolved gap the code itself
  already admitted to (`_build_create_chat_command`'s own comment: "whether
  `/welcome` resolves the same way -- or at all -- on the other three
  harnesses is unconfirmed"). Resolved by actually checking each harness's
  real skill-invocation convention rather than leaving it as a stated
  unknown:
  - **claude**: `/welcome` -- established convention (skill `name` ->
    `/name`).
  - **antigravity**: confirmed the SAME `/name` convention via a real
    source (not assumed just because claude works this way) -- no fix
    needed, `/welcome` already worked here.
  - **codex**: confirmed via developers.openai.com/codex/skills that codex
    does NOT support `/<skill-name>` -- its explicit-invocation syntax is
    `$<skill-name>` (a mention); `/skills` opens a picker, not a direct
    named invocation. Sending literal `/welcome` would have just been
    meaningless text to codex. Fixed to `$welcome`.
  - **opencode**: confirmed NO manual slash/mention invocation exists at
    all -- skills load only via the model calling a `skill({name: ...})`
    tool itself, driven by automatic relevance-matching. `/welcome` would
    have done nothing useful. Fixed to a plain-English instruction
    ("Use the welcome skill to greet the user.") that echoes the skill's
    own description, to maximize automatic-match odds -- stated plainly as
    inherently less deterministic than the other three, not presented as
    a confirmed-equivalent fix.
  - Implementation: `libs/bootstrap/src/bootstrap/manager.py` gained
    `_WELCOME_MESSAGE_BY_HARNESS` + `_resolve_welcome_message()`, mirroring
    the existing `_CHAT_TEMPLATE_BY_HARNESS`/`_resolve_chat_template()`
    pattern exactly; `_build_create_chat_command` now calls it instead of
    hardcoding `/welcome`.
  - **Also fixed a stale, pre-existing test found along the way**:
    `test_build_create_chat_command_includes_welcome_and_template` asserted
    `--template == "chat"` -- the pre-multi-harness template name, already
    wrong before this change (the real value has been `chat_claude` since
    the earlier harness-dispatch work, but no test ever caught the drift
    because `_resolve_chat_template`/`_resolve_welcome_message` had zero
    committed test coverage until now). Added real parametrized tests
    (`_resolve_chat_template`/`_resolve_welcome_message` across all four
    harnesses + unset + garbage-value fallback) plus a codex-specific
    `_build_create_chat_command` test. Verified via the same standalone
    smoke-test workaround established earlier this session (this repo's
    `uv run pytest` can't resolve the full monorepo dependency graph
    standalone) -- all combinations correct, including both fallback
    paths. `uv.lock` drift from the attempted real pytest run reverted,
    same as before.

- **Full workflow-backed code review (xhigh effort) of Phases 1-3, at the
  user's explicit request, with fixes applied.** 6 independent finder
  agents + a verifier per candidate + a synthesis pass; 22 candidates
  verified, 1 refuted, 15 real distinct defects confirmed. All 15 fixed in
  this pass (not just reported) -- see `ReportFindings` output earlier in
  this session for the full finding text; summary of the fixes:
  - **Three real regressions on previously-working claude functionality**,
    the most severe findings: `create_templates.chat/worktree` and
    `worker/subskill-worker` lost their bare (harness-unsuffixed) names
    with no alias, breaking system_interface's New Chat/New Worktree UI
    buttons and six delegation skills (launch-task, crystallize-artifact,
    update-artifact, heal-artifact, update-system-interface,
    use-ai-integration) for ALL harnesses including claude; `[commands.create]`'s
    `type = "claude"` default was removed with no replacement, breaking
    every real minds-desktop-client "create workspace" click (Docker/Lima/
    Vultr/AWS/imbue_cloud), which never passes `--type` explicitly. Fixed
    by restoring the command-level default and adding bare-named
    `create_templates.chat`/`worktree`/`worker`/`subskill-worker` as real
    duplicates of the `_claude` variant (TOML/mngr templates don't support
    inheritance) -- old callers keep working via the bare name, new
    harness-aware callers use the suffixed name.
  - **Settings.toml couldn't actually be parsed by the `mngr` CLI tool
    itself**: `scripts/build_workspace.sh`'s `mngr plugin add` never
    registered mngr_codex/mngr_antigravity/mngr_opencode (only
    mngr_claude/mngr_wait), and `uv.lock` was never regenerated after
    `pyproject.toml`'s dependency additions. Fixed: added the three
    `--path` entries (confirmed repeatable via `mngr plugin add --help`),
    ran `uv lock` (regenerated cleanly, `uv lock --check` now passes).
  - **A real, separate data-loss bug found investigating the above**: the
    Dockerfile's codex/antigravity/opencode `ARG`/`ENV`/`COPY` lines (PATH,
    version pins, pre-COPY manifest layer) had gone missing entirely --
    traced to an earlier scratch-git-branch test in this same session
    (`git checkout -b`, commit, delete branch) silently discarding
    uncommitted Dockerfile edits that were never committed to `main`.
    Restored in full, matching the version that produced this session's
    earlier successful `docker build`.
  - **codex's sandbox silently blocked all network access** (`git push`,
    `uv sync`, package installs) regardless of `auto_allow_permissions`,
    since `sandbox_mode = "workspace-write"` defaults network off at the OS
    level independent of approval policy. Fixed: added
    `sandbox_workspace_write.network_access = true` (confirmed real schema
    via docs + a live GitHub issue discussion, not guessed).
  - **The git-rewrite guard (all 4 variants: claude original + codex/
    antigravity/opencode ports) was trivially bypassable** by chaining --
    `git add -A && git commit --amend` never matched a `^git`-anchored
    regex. Fixed in all four to also match right after a shell chain
    operator (`&&`, `;`, `|`), not just at the very start of the string --
    real bash regex bug caught empirically mid-fix (a zsh-vs-bash `[[ =~ ]]`
    engine difference in my own testing shell, not a real bug in the
    pattern), verified in real bash after.
  - **`mngr_antigravity`'s per-agent MCP config / GEMINI.md never got
    deleted on a re-provision that unset the field** -- contradicted the
    method's own "idempotent each provision" docstring. Fixed with an
    explicit `rm -f` (matching an existing precedent in the same file),
    2 new re-provision tests added, full suite re-run (135 passed),
    synced to `vendor/mngr`.
  - **`antigravity_update_plugin_preinvocation.sh` marked itself "done"
    even when the install failed** (`|| true` swallowed the exit code),
    permanently skipping retry. Fixed to only write the marker on real
    success: verified both paths with a stubbed `agy` function.
  - **The tk/ticket-invocation skip pattern in the codex and antigravity
    step hooks had two real bugs**: a catch-all glob alternative false-
    negatived on any command merely containing the substring "tk " (e.g.
    `apt-get install python3-tk`), and neither recognized a bare
    `ticket ...` invocation (no leading slash) the way opencode's regex
    port already did. Fixed by switching both to the same regex opencode
    uses (`(^|/|\s)(tk|ticket)\s`), verified against both failure
    scenarios plus the original working cases.
  - **Three opencode plugins used `??` instead of `||`** for the
    `TICKETS_DIR` env-var fallback, so an empty-string (not just unset)
    value silently disabled the hook -- unlike the bash ports'
    `${TICKETS_DIR:-default}`, which falls back on empty too. Fixed all
    three, verified live.
  - **`open-tickets-stop-nudge.ts`'s `isRootSession` couldn't distinguish
    "session never observed" from "confirmed root"** (`Map.get()` returns
    `undefined` for both), risking a sub-agent session being misattributed
    as root after a plugin reload. Fixed to check `.has()` first and treat
    an unobserved session as non-root (a missed nudge is a smaller failure
    than a misattributed one). Verified with all three cases.
  - **The memory MCP server config is hand-duplicated 4x** (`.mcp.json` +
    3 `settings.toml` blocks) with no single source of truth -- TOML has
    no anchor mechanism and JSON has no comment syntax, so true dedup
    isn't achievable without new tooling. Added explicit cross-referencing
    comments at all 3 TOML locations (JSON can't hold one) as the
    pragmatic mitigation.
  - **Built as a follow-up, at the user's request**: the memory-MCP-tool-call
    reminder hook described above. Two reminders, not one: "search memory
    for context" (fires once per fresh session) and "consider saving what
    you learned" (fires recurring, at the start of each subsequent turn).
    Corrected an assumption caught before implementing, not after: the
    initial plan was SessionStart + Stop, but Stop can't actually deliver
    a model-visible reminder without blocking (its only content channel is
    `exit 2`, forcing the agent to continue -- confirmed via
    `claude_open_tickets_stop_nudge.sh`'s own comment that its Stop
    message is "mainly for orchestrator log / human visibility", never
    seen by the model). Used `UserPromptSubmit` instead for the "save"
    side -- the same real mechanism already proven for the tk-steps
    reminder (hook #5), not a new pattern.
    - **claude**: `scripts/claude_memory_reminder_sessionstart.sh`
      (`SessionStart`) + `scripts/claude_memory_reminder_userpromptsubmit.sh`
      (`UserPromptSubmit`), wired into `.claude/settings.json` alongside
      the existing hooks in each event.
    - **codex**: same shape, direct port -- `scripts/codex_memory_reminder_sessionstart.sh`
      (`SessionStart`) + `scripts/codex_memory_reminder_userpromptsubmit.sh`
      (`UserPromptSubmit`), wired into `.codex/hooks.json` alongside the
      existing hooks in each event; both events and the `additionalContext`
      mechanism confirmed identical to claude's.
    - **antigravity**: one combined `PreInvocation` hook
      (`scripts/antigravity_memory_reminder_preinvocation.sh`) -- no
      `SessionStart` event exists, so the "search" text fires once per
      agent lifetime (marker-file gated, same pattern as
      `antigravity_update_plugin_preinvocation.sh`) and the "save" text
      fires on every subsequent invocation. This is now the third
      `PreInvocation` hook group registered for antigravity (alongside
      `mngr-open-tickets-reminder` and `mngr-plugin-update`) -- whether
      agy merges multiple `PreInvocation` hooks' JSON outputs or only
      honors the last one is still not confirmed empirically; same
      accepted degradation as noted for the first two (worst case one is
      silently dropped some turns, acceptable for advisory-only hooks).
    - **opencode**: `.opencode/plugin/memory-reminder.ts`, same
      `experimental.chat.messages.transform` mechanism as the tk-steps
      port, tracking a per-session `Set` (search once per session) plus
      the existing per-message-id dedup guard (avoid double-injecting into
      one message). Same experimental-namespace/unconfirmed-firing-frequency
      caveats already disclosed for that mechanism.
    - All 5 new bash/JSON files validated (syntax, JSON parse) and
      functionally tested with real hook-shaped input (confirmed correct
      output shape and correct once-vs-recurring gating on both the
      antigravity marker-file path and the opencode per-session Set path).
      The opencode plugin passes esbuild with 0 errors; not exercised
      against a live opencode session.

## Phase 4, task 1: harness visibility in system_interface

Before this, system_interface's UI had no way to tell a codex/antigravity/
opencode agent apart from a claude one -- mngr's `AgentDetails.type` (e.g.
`codex-worker`) was already present in the API response but never surfaced
past `agent_discovery.py`. Added `imbue/system_interface/harness.py`
(`Harness` enum + `parse_harness()`, strips the `-main`/`-worker` role
suffix and maps the base string to an enum member, `None` for anything
unrecognized) and threaded a `harness: Harness | None` field through
`AgentInfo` -> `AgentStateItem`/`AgentListItem` -> the `/agents` list
endpoint, across all 7 construction sites in `agent_manager.py`
(`get_agent_info_by_id`, `_initial_discover`, `_refresh_agents`,
`_run_creation`, `_handle_discovery_event`) plus `agent_discovery.py` and
`server.py`. `_run_creation` hardcodes `Harness.CLAUDE` -- its only two
callers (`create_worktree_agent`/`create_chat_agent`) always request the
`worktree`/`chat` templates, which alias to claude only today; generalizing
this is Phase 5 (harness-picker UI) scope, not this change.

Also closed the `--append-system-prompt` gap (functionality-matrix row
3.20): codex/antigravity/opencode have no confirmed, safe equivalent CLI
flag for appending to a running agent's system prompt. Replaced with a
harness-agnostic mechanism instead -- `create_worker.py`'s `launch()` now
prepends a `LAUNCHED_BY_ANOTHER_AGENT_PREAMBLE` constant to the task file
content and sends both in one `mngr message --message-file` call (merged
into the existing task-delivery call, not a second CLI dispatch), for every
worker on every harness. Verified safe re: YAML frontmatter positioning --
the worker re-reads its real task file fresh from its own rsynced runtime
dir via a glob pattern (`parse_task_frontmatter.py`), never by parsing the
literal chat message text, so prepending prose ahead of the frontmatter
does not break report-path resolution.

Tests: `harness_test.py` (13 cases, all 4 harnesses x claude/main/worker
suffixes + unrecognized-type fallback), `create_worker_test.py` updated for
the merged single-call send (46 passed), `agent_manager_test.py` (95
passed) covering the harness-threading and the `_harness_by_agent_id`
per-discovery-event cache added during the follow-up simplify pass.

## Phase 4, task 2: chat transcripts for the other three harnesses

`AgentSessionWatcher` only ever parsed Claude's raw session JSONL --
codex/antigravity/opencode chat agents rendered a permanently empty
transcript, even though mngr already writes a harness-agnostic transcript
for all four (`imbue.mngr.agents.common_transcript_records`: each plugin's
own converter normalizes its native CLI output to a shared
`user_message`/`assistant_message`/`tool_result` schema at
`events/<harness>/common_transcript/events.jsonl`; `mngr transcript`
already reads it). Rather than rewrite `AgentSessionWatcher` or duplicate
mngr's per-harness CLI-wrapping work, added a second, much smaller reader
(`common_transcript_watcher.py`) that reuses mngr's existing
`discover_event_sources`/`read_all_historical_events` API to poll that
already-written file and adapts each record into the same event-dict shape
`session_parser.py` produces for Claude, so the frontend renders it
identically regardless of harness. No byte-offset locator index or
subagent linkage (neither exists in the shared schema, and these harnesses'
transcripts are small) -- a poll loop re-reads the whole file and diffs
against seen event ids. Claude keeps `AgentSessionWatcher` unchanged.

Added `transcript_watcher.py`: a `TranscriptWatcher` Protocol (the two
watchers' shared interface) and `build_transcript_watcher(agent_info,
on_events)`, the one place that routes on `agent_info.harness` -- claude
(or an unrecognized/`None` harness, preserving prior behavior) gets
`AgentSessionWatcher`, the other three get `CommonTranscriptWatcher`. Wired
into both watcher construction sites: `app_context.py` (the live chat view)
and `welcome_resend.py`, which previously read `AgentSessionWatcher`
unconditionally too -- meaning the initial-chat-agent welcome-resend check
was silently broken for any non-claude chat agent (it would always see an
empty transcript and resend needlessly). `agent_discovery.py`'s
`_get_mngr_context` was promoted to `get_mngr_context` (no longer private)
since the new watcher needed the same short-lived-context pattern.

**Live-verified, not just read**: ran mngr's own `pytest.mark.release`
end-to-end lifecycle tests (real CLI, real create/message/stop/resume/
destroy) against this exact mechanism. codex's run hit an expired
`~/.codex/auth.json` refresh token (confirmed independently via a direct
`codex exec` call, `401 token_expired`) -- unrelated to this change: codex's
own transcript plumbing round-tripped its `session_meta`/user-turn correctly
up to that point, it just correctly recorded that the model produced no
reply once auth failed. opencode's run passed clean end-to-end, including
the schema-conformance assertion on the emitted common-transcript records
and the stop/resume/adopt-from-preserved arc. antigravity was not
separately live-tested (same generic test harness and schema as the other
two, and mngr's own `common_transcript_convert_test.py` covers its
converter) -- flagged here rather than silently assumed.

Tests: full `system_interface` suite (559 passed, 84.68% coverage) after
the change; bumped the `check_init_methods_in_non_exception_classes`
ratchet 5 -> 6 (`CommonTranscriptWatcher.__init__` follows the same
thread-plus-lock shape as `AgentSessionWatcher`, not a natural fit for a
Pydantic model, matching the existing precedent for that ratchet).

## Phase 4, task 3: auth-status parity (detection only, not recovery)

The remaining Claude-only backend piece from the Phase 4 audit --
`claude_auth.py`/`claude_auth_endpoints.py`/`claude_auth_patterns.py` --
is a workspace-wide (not per-agent) service: it drives `claude auth login`
via a PTY, restarts every `type: claude` agent on a new credential, and
backs a single app-level login-recovery modal. Porting the *interactive
recovery* flow (3 more OAuth/API-key UIs, one genuinely different per
harness -- codex's ChatGPT OAuth, antigravity's, opencode's multi-provider
`opencode auth login`) is real, harness-specific product surface, not
"minimal new tooling" -- that's Phase 5 (Authentication UX) work, already
scoped there as its own sub-project, and was left alone.

What *does* fit this pass, and closes a real correctness gap: the existing
`is_auth_error` signal (drives both the login-modal trigger and hiding
pre-recovery noise turns, see `message-renderers.ts`) was `False` by
construction for every non-claude event -- meaning (a) a real codex/
antigravity/opencode auth failure surfaced with zero signal to the user
(the exact "silently stuck chat" experience hit personally while
live-verifying the transcript work above, via an expired codex token), and
(b) had it been wired naively, it would have opened the Claude-only
recovery modal for a non-claude failure -- offering to run `claude auth
login` to fix a codex problem.

Fixed both: added `common_transcript_auth_patterns.py` (mirrors
`claude_auth_patterns.py`'s structure, keyed by harness) and wired real
detection into `common_transcript_watcher.py`'s event mapper, keyed off
the record's own `source` field (`"<harness>/common_transcript"`) rather
than threading a harness parameter through construction. Seeded only with
codex's live-confirmed error text (`token_expired`, `401 Unauthorized`,
etc., observed via the release-test's expired-token failure above) --
antigravity/opencode are left with no patterns rather than guessed ones;
a wrong-but-confident pattern is worse than no detection. Also flagged:
codex's headless `exec` mode failure I hit didn't even produce assistant
text (`task_complete` with `last_agent_message: None`) -- a distinct
"silent empty completion" failure shape text-matching can't see, left as
a known gap rather than papered over.

Separately fixed the frontend risk directly: `AgentState` (frontend) gained
the `harness` field the backend was already sending but the frontend never
declared, and `ChatPanel.ts`'s `checkLatestAssistantForAuthError` now only
opens the Claude recovery modal when the agent's harness is claude (or
unrecognized/null, matching the backend's own fallback) -- so even a future
false-positive detection can no longer point a non-claude user at the wrong
fix. The decision itself is a pure, exported predicate --
`models/ClaudeAuth.ts`'s `shouldOpenLoginModalForHarness` -- rather than
inlined in the mithril component, so it's directly unit-tested
(`ClaudeAuth.test.ts`) without mounting `ChatPanel`.

Tests: `common_transcript_watcher_test.py` (new, 14 cases covering the
event mapper, usage/tool-call normalization, and auth-pattern wiring) +
`common_transcript_auth_patterns` cases within it. Full backend suite: 574
passed, 85.23% coverage. Frontend: `npx tsc --noEmit` clean, `npm test`
209 passed (prettier also caught and fixed pre-existing, unrelated drift
in `ProtoAgentLogView.ts`).

## Phase 6: delegation routing (worker harness parity)

`create_worker.py` (the one shared choke point all six delegation skills
launch workers through -- `launch-task`, `crystallize-artifact`,
`update-system-interface`, `update-artifact`, `use-ai-integration`,
`heal-artifact`) always requested a bare `worker`/`subskill-worker`
template, which aliases to claude. A codex/antigravity/opencode agent
delegating a sub-task got a claude worker regardless.

The templates to fix this already existed and were unused:
`.mngr/settings.toml` has had `worker_claude/codex/antigravity/opencode`
and `subskill-worker_claude/codex/antigravity/opencode` since earlier in
this project, built for exactly this purpose but never wired up. So the
fix needed no new template, no new CLI flag on the skill side, and no
change to any of the six skills' prose (they all still say `--template
worker`) -- only `create_worker.py` needed to learn which variant to
actually request.

Added three small functions:

- `_resolve_delegating_harness(state_dir)` -- reads the *delegating* (lead)
  agent's own `data.json` (at `$MNGR_AGENT_STATE_DIR`, already read
  elsewhere in this script for the common-transcript flush) and returns its
  `type` field's harness, via `_parse_harness`.
- `_parse_harness(raw_agent_type)` -- the same `-main`/`-worker`
  suffix-stripping as `apps/system_interface/imbue/system_interface/
  harness.py`'s `parse_harness`. Duplicated rather than imported:
  `create_worker.py` is a standalone `uv run --script` file (PEP 723
  inline deps, just `pyyaml`) with no dependency on the system_interface
  package, and this is five lines.
- `_resolve_template(template, state_dir)` -- suffixes `template` with the
  detected harness (`worker` -> `worker_codex`). Falls back to `template`
  unchanged when the harness can't be determined (no `data.json`, malformed
  JSON, non-string/unrecognized `type`) -- exactly today's prior behavior,
  so nothing regresses for a caller not running under mngr.

`launch()` now resolves the template once, right before the `mngr create`
call, so every one of the six skills gets harness-correct delegation for
free.

Live-verified, not just read: all six new template names resolve via
`mngr config get create_templates.<name>` against the live CLI, each
reporting the expected `type` (e.g. `worker_codex` -> `"type":
"codex-worker"`).

Tests: 19 new cases added to `create_worker_test.py` (`_parse_harness`
parametrized over all 4 harnesses x role suffixes plus an unrecognized
type; `_resolve_delegating_harness`'s three fallback-to-None paths --
missing state_dir, missing data.json, malformed JSON, unrecognized type;
`_resolve_template`; and two `launch()`-level integration tests confirming
the actual `mngr create -t <template>` argv). 65 passed total (was 46), all
pre-existing tests unchanged (none of their fixtures write a `data.json`,
so they exercise the unchanged fallback path). `mypy` clean.

## Phase 7: harness picker on user-initiated agent creation

The other creation path that still always produced claude: the "+" button's
"New chat"/"New agent" (worktree) menu items, which hardcode `--template
chat`/`--template worktree` in `agent_manager.py` (both alias to claude).
Scoped explicitly to *creation* only, on the assumption (per the user's
instruction) that the underlying terminal/host is already authenticated for
whichever harness is picked -- Phase 5's interactive auth recovery flows
remain untouched and out of scope.

**UI**: the "New chat"/"New agent" dropdown items are no longer directly
clickable -- which harness either would mean is ambiguous -- and instead
expand a submenu (Claude/Codex/Antigravity/Opencode) on hover, extending
`DockviewWorkspace.ts`'s existing raw-DOM "+" dropdown with a `children`
field on `DropdownItem` (a parent with `children` renders via
`buildSubmenuParentItem`, CSS `:hover` reveals `.dockview-add-tab-submenu`,
opening leftward since the outer dropdown is right-aligned and a rightward
flyout would tend to run off-screen). Picking a harness opens the existing
name-input `CreateAgentModal` with that harness pre-selected (title updates
to e.g. "Create Codex Chat Agent") and threads it into the create POST body.

**Backend**: `chat_<harness>`/`worktree_<harness>` templates already
existed (same pattern as Phase 6's worker templates, live-confirmed via
`mngr config get create_templates.<name>`). Added an optional `harness`
field to `CreateChatRequest`/`CreateWorktreeRequest` (a pydantic `Harness`
enum -- an unrecognized value 400s automatically, no extra validation code
needed) and a `_resolve_create_template(base, harness)` helper mirroring
`create_worker.py`'s Phase 6 pattern, threaded through
`_build_chat_create_command`/`_build_worktree_create_command` ->
`create_chat_agent`/`create_worktree_agent` -> `_launch_creation_thread` ->
`_run_creation`. The last hop fixes a real (if narrow) bug: `_run_creation`
previously hardcoded `harness=Harness.CLAUDE` on the resulting
`AgentStateItem` with a comment noting this was accurate only because no
caller could request otherwise -- now `harness or Harness.CLAUDE`, so the
UI correctly labels a picked-Codex agent as Codex once creation completes
(this is what other UI, e.g. the eventual auth-error routing, reads to know
an agent's harness).

**Verification**: found and followed a real project rule
(`update-system-interface/SKILL.md`'s "never edit the served tree
directly") that turned out not to fit this session -- this is template-repo
development, not a live deployed mind with a real UI at risk -- confirmed
with the user before proceeding with direct local verification instead of
the full worker/preview/reveal ceremony. Built the frontend for real
(`npm run build`, not just `tsc --noEmit`) and confirmed the compiled
bundle contains the new submenu code; booted the real Flask backend
against local `~/.mngr` state and confirmed via `curl` that a `harness`
field parses through to the same "missing primary agent work dir"
precondition a no-harness request hits (proving the new field doesn't
crash the handler or change what fails) and that an invalid harness value
gets a clean 400 rather than a 500. Could not do a full live "click through
and see a real Codex agent get created" pass -- no browser/screenshot
tooling was available in this session, and reaching a real primary-agent
context standalone (outside a real mngr-provisioned host) is a separate,
disproportionate setup; flagged rather than silently skipped.

Tests: 5 new cases in `agent_manager_test.py` (`_resolve_create_template`
parametrized over all 4 harnesses; harness-suffixed argv for both builders,
live-CLI-validated; `_run_creation` recording the requested harness on the
resulting `AgentStateItem`, and defaulting to claude when none given) + 4
new cases in `server_test.py` (harness field parses through to the same
precondition failure; invalid harness 400s cleanly for both endpoints).
Backend: 592 passed (was 574), 85.28% coverage. Frontend: `npm run build`
clean, `tsc --noEmit` clean, `npm test` 209 passed.

## Workflow-backed code review (xhigh) of the staged multi-harness work

Ran across the whole staged diff (Phases 4/6/7, `git diff --cached`), not
just the latest edit -- first launch scoped against the wrong directory
(`/Users/minh/code/rally_sonnet`, not a git repo at all) and correctly
declined to guess rather than silently reviewing nothing useful; re-scoped
explicitly to `~/imbue/forever-claude-template`'s staged changes. 7 findings
survived independent verification; all 7 addressed (fixed 6 real issues, 1
was a git-staging artifact -- the fix already existed in the working tree,
just never re-`git add`-ed after editing).

- **`common_transcript_watcher.py`'s `is_auth_error` hardcoded to `False`**
  -- confirmed as a stale index snapshot, not a code bug: the working tree
  already had the `is_auth_error_text` call, it just hadn't been re-staged
  since. Re-staged.
- **`get_mngr_context()` called unguarded, every poll tick.** Two real bugs
  from one root cause: (1) any exception it raised propagated out of
  `_poll_once`/`_run` uncaught, permanently killing that agent's background
  polling thread with no recovery short of restarting the whole server; (2)
  it re-did a full `load_config` disk read + `ConcurrencyGroup`
  construct/teardown every `POLL_INTERVAL_SECONDS` (1s) for as long as any
  non-Claude chat panel stayed open, unlike the one-shot callers this
  helper was designed for. Fixed both together: the watcher now acquires
  the mngr context once (`_ensure_mngr_context`), holds it for its whole
  lifetime (mirrors `AgentManager._creation_cg`/`_observe_cg`'s existing
  long-lived-`ConcurrencyGroup` pattern), retries on a transient acquisition
  failure instead of giving up permanently, and releases it in `stop()`.
- **`agent_manager.py`'s harness cache wrote a permanent `None`** when an
  agent's first discovery event(s) arrived before its `data.json`'s `type`
  field was populated (`DiscoveredAgent.agent_type` is legitimately `None`
  during that transient window, not a real "unknown harness"). A
  codex/antigravity/opencode agent caught in that window would be
  permanently mislabeled as claude -- misrouting its transcript watcher and
  the frontend's auth-modal gating -- for its whole tracked lifetime. Fixed
  to keep retrying resolution each event until it succeeds, caching only a
  real resolution, never the transient `None`.
- **`create_worker.py`'s merged-message tempfile never got cleaned up.**
  The comment justifying that (deleting it might race `mngr message`
  reading it) was actually wrong: `Runner.run` is a blocking call, so by
  the time it returns the file has already been read (or the launch has
  already failed loudly) -- deleting it right after is safe. Every worker
  launch, across all six delegation skills and all four harnesses, was
  leaking one `.md` file into the OS temp dir. Fixed with a `finally:
  combined_path.unlink(missing_ok=True)`; updated `_RecordingRunner` (the
  test double) to snapshot the file's content at call time (mirroring when
  a real subprocess would read it) so tests can still assert on it after
  `launch()` returns and the file is gone.
- **The "New chat"/"New agent" submenu is hover-only, stranding touch
  input** (no true `:hover` state on a tap). This mechanism -- the parent
  item not being directly clickable -- was an explicit design requirement
  ("ambiguous which harness"), so the fix isn't a revert: clicking the
  parent now also toggles a `.submenu-open` class that reveals the same
  submenu the CSS `:hover` rule does, so a tap can reach it too. The parent
  still never creates an agent by itself either way -- only a leaf item
  does.
- **`MULTI_HARNESS_FUNCTIONALITY_MATRIX.md` used emoji status markers**
  throughout, against this repo's own CLAUDE.md ("Never use emojis. Remove
  any emojis you see in ... docs whenever you are modifying ... those
  docs."). Replaced with `[DONE]`/`[PARTIAL]`/`[N/A]`/`[TBD]` throughout;
  also cleaned up two spots where the substitution produced a redundant
  "`[N/A] N/A --`".

Tests: 1 new case in `agent_manager_test.py` (harness resolution recovers
once a delayed `type` field arrives, instead of staying stuck at `None`); 1
new case in `create_worker_test.py` (the tempfile is actually gone after a
successful launch) plus the two existing message-content assertions
rewritten against the runner's call-time snapshot instead of a post-hoc
re-read of a now-deleted path. Backend: 593 passed (was 592). Frontend:
`tsc --noEmit` clean, `npm test` 209 passed (unchanged count -- the
touch-fallback fix had no dedicated test added, since `DockviewWorkspace.ts`
has no existing test file/pattern to extend, matching the precedent noted
in Phase 4's auth-detection work). `create_worker_test.py`: 66 passed (was
65).

## Correction: antigravity's `global_instructions_md` was never needed

The earlier `mngr_antigravity` entry below (the `AGENTS.md` / `GEMINI.md`
section) said agy "has NO per-project AGENTS.md/CLAUDE.md auto-discovery of
its own." That claim was never checked against the real, installed `agy`
binary -- only inferred. Pushed as part of a real deployment attempt this
branch was actually spun up against, which surfaced it: `mngr create`
rejected `.mngr/settings.toml`'s `agent_types.antigravity` block with
`Unknown fields: ['global_instructions_md', 'mcp_servers']` when built from
a fresh `origin/main` clone of `imbue-ai/mngr` -- the field only ever
existed in a local, unpushed checkout.

Fixing that surfaced the deeper issue: `strings` on the installed `agy`
binary shows its own bundled documentation directly contradicts the
original claim -- "The system walks up from the current working directory
to the repository root" discovering `GEMINI.md`/`AGENTS.md`, the same
hierarchical discovery codex/opencode/claude already have for
`AGENTS.md`/`CLAUDE.md`. The `global_instructions_md` field (writing a
"please check AGENTS.md" instruction to a per-agent `$HOME/.gemini/GEMINI.md`)
was solving a problem that didn't exist -- agy already finds the project's
real `AGENTS.md` on its own. Not a functional bug (the redundant global file
was simply never read, since it sits outside the directory-walk agy actually
does), but real, shipped inaccuracy.

Removed entirely rather than left misdocumented: the `global_instructions_md`
field, `get_antigravity_global_instructions_path()`, its three dedicated
tests, and `.mngr/settings.toml`'s usage of it. `mcp_servers` is unaffected
and separately re-verified correct -- directly against the binary
(`"mcpServers"` JSON key literal, `mcp_config.json` path) and cross-checked
against the real `~/.gemini/config/mcp_config.json` file that mechanism
produces on this machine's actual agy install.

Pushed as a second commit on `imbue-ai/mngr@multi-harness-support`
(`8031b80`), tested against a fresh worktree off `origin/main` before
pushing (132 passed, 0 failed -- down from 135 by the 3 removed tests, no
new failures). fct's `vendor/mngr` copy and `.mngr/settings.toml` updated to
match and re-validated by loading the real settings.toml through the fixed,
freshly-built `mngr` binary (`agent_types.antigravity` resolves with
`mcp_servers` present and `global_instructions_md` absent, confirmed via a
direct field-presence check, not just "no error printed").

## Open issues

Genuine dead ends, confirmed by testing against the live installed CLIs
(codex 0.142.5, antigravity/agy 1.0.16, opencode 1.17.13) -- not just
unresearched:

- **antigravity has no per-tool disallow-list, and no workaround either.**
  Its real built-in tool names (the AskUserQuestion/TodoWrite equivalents)
  could not be enumerated even with live shell access on an authenticated
  install -- they derive from internal constants unreachable short of
  disassembly or an interactive probing session. Mitigated with a
  system-prompt instruction for now (see `SYSTEM_PROMPT_NOTES.md`).
- **antigravity has no version-pinning capability at all.** Google's
  installer always installs latest from a manifest -- no arg, no env var.
  `update_policy = "NEVER"` freezes the installed *build* but can't pin a
  specific version number.
- **codex's `update_plan` tool (its TodoWrite equivalent) has no dedicated
  feature flag.** Unlike `multi_agent`/`apps`/etc, which are real,
  individually-toggleable `codex features list` entries, `update_plan`
  can't be disabled the way TodoWrite can on claude. Mitigated with a
  system-prompt instruction for now (see `SYSTEM_PROMPT_NOTES.md`); a
  `PreToolUse`-hook-based real fix may be possible but is unconfirmed --
  codex's hook docs only explicitly listed Bash/apply_patch/MCP tool
  coverage, not `update_plan` specifically.
- **No harness besides claude supports a live shared config dir.** Codex,
  antigravity, and opencode all hardcode full per-agent isolation with no
  native toggle -- confirmed absent in each plugin's full field list, not
  just undocumented. Only auth propagates (each harness has its own
  narrower auth-symlink mechanism). Remediable in principle via a custom
  `extra_provision_command` copy-step, but NOT implemented -- would need
  bootstrap to expose each harness's "-main" agent's config-dir path as a
  source first, which it doesn't.

Unbuilt work, deliberately out of scope for this change:

- `create_worker.py` / the `launch-task` skill have no harness routing --
  delegation always produces a claude worker regardless of which top-level
  agent initiated it. Needs a `--harness` arg or equivalent.
- The `--message "/welcome"` skill invocation in `_build_create_chat_command`
  is unconfirmed to resolve the same way (or at all) on the other three
  harnesses -- left as-is pending verification rather than guessing a
  harness-specific alternative.

Minor, low-risk, worth a final pass before relying on them:

- codex's `config_overrides` keys (`analytics`, `feedback`,
  `check_for_update_on_startup`) were confirmed via `codex app-server --help`
  and binary strings, not cross-checked against a rendered docs page.
- opencode's `openTelemetry` suppression may still leak (live GitHub issue
  #5554); no env-var kill switch exists for it specifically, unlike
  `autoupdate`/`share` which do have `OPENCODE_DISABLE_*` alternatives.
- opencode's tool-disallow name mapping to claude's list is inexact by
  necessity, not a bug: `todowrite`~TodoWrite, `question`~AskUserQuestion,
  `task`~all of TaskCreate/TaskList/TaskUpdate rolled into one tool, no
  `ExitPlanMode` analog exists to disable in the first place.

See `SYSTEM_PROMPT_NOTES.md` for the system-prompt-level mitigations noted
above, and this changelog's git history for the design discussion.
