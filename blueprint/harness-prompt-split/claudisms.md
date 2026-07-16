# Claudisms

The harness-neutral body of the old `CLAUDE.md` now lives in `AGENTS.md` (read natively
by codex and every other AGENTS.md harness). `CLAUDE.md` is `@AGENTS.md` plus the
Claude-specific delta. This file records each Claude-specific quote from the original
`CLAUDE.md`, what `AGENTS.md` says instead, and what moved to `CLAUDE.md`.

The Codex column is TBD -- to be filled in once we settle each equivalent.

---

## (1) TodoWrite disabled

**Original quote** (line 21):
> You manage your work using `tk`, the vendored ticket tracker at `vendor/tk/`. It is the **only** task tracker available — Claude Code's built-in `TodoWrite` is disabled.

**In AGENTS.md:**
> It is the **only** task tracker available — any of your built-in todo tools are disabled.

**In CLAUDE.md:**
> Claude Code's built-in `TodoWrite` is disabled, use tk instead.

**Codex equivalent:** TBD -- codex's built-in planning tool is `update_plan`. Open question:
is it disabled at the tool level (like claude's `--disallowed-tools TodoWrite`), or does it
need a prompt line telling codex not to use it?

---

## (2) Self-modification target

**Original quote** (line 199):
> - **CLAUDE.md**: (this file) update these instructions if you discover better ways to operate.

**In AGENTS.md:**
> - **AGENTS.md**: (this file) update these instructions if you discover better ways to operate.

**In CLAUDE.md:** nothing (the AGENTS.md line covers it -- for the shared file, "this file"
*is* AGENTS.md).

**Codex equivalent:** TBD -- probably a pointer that codex-specific instructions live in
`<repo>/.codex/AGENTS.md` (which provisioning copies to `$CODEX_HOME/AGENTS.md`), so codex
edits the committed source rather than the runtime copy.

---

## (3) .claude/skills symlink

**Original quote** (line 200):
> - **.agents/skills/**: Create new skills or modify existing ones. Each skill is a directory with a SKILL.md file. (Also symlinked from `.claude/skills/`.)

**In AGENTS.md:** the parenthetical is deleted --
> - **.agents/skills/**: Create new skills or modify existing ones. Each skill is a directory with a SKILL.md file.

**In CLAUDE.md:**
> `.claude/skills` does not exist, it is symlinked to `.agents/skills/`.

**Codex equivalent:** none needed -- codex reads `.agents/skills/` natively (no symlink in
play). Nothing to say.

---

## (4) Memory

**Original quote** (lines 229-230):
> # Memory
>
> Use Claude's built-in memory system. Your memory directory is `runtime/memory/` (configured via `autoMemoryDirectory` in `.claude/settings.json`).
> Memory is gitignored from the main branch. When the user has enabled GitHub sync (the `github-sync` skill), the github-sync service ships it -- with the rest of `runtime/` -- to the `runtime-sync` branch of the workspace's private sync repo, so it survives container loss.

**In AGENTS.md:** the Claude-specific sentences are removed; the github-sync sentence is
harness-neutral and stays. Its "ships **it**" referred to Memory, which left with the moved
text, so the antecedent is restated:
> # Memory
>
> When the user has enabled GitHub sync (the `github-sync` skill), the github-sync service ships `runtime/memory/` -- with the rest of `runtime/` -- to the `runtime-sync` branch of the workspace's private sync repo, so it survives container loss.

**In CLAUDE.md:**
> # Memory
>
> Use Claude's built-in memory system. Your memory directory is `runtime/memory/` (configured via `autoMemoryDirectory` in `.claude/settings.json`). Memory is gitignored from the main branch.

**Codex equivalent:** TBD -- this is the one clear-cut gap. Codex has no auto-memory, so it
likely needs an explicit instruction to read `runtime/memory/*.md` at session start and to
persist durable notes there.

---

## (5) "for Claude memory" in the runtime parenthetical

**Original quote** (line 244):
> `runtime/` is gitignored from the main branch (it includes `runtime/memory/` for Claude memory and other transient state).

**In AGENTS.md:** the word "Claude" is removed --
> `runtime/` is gitignored from the main branch (it includes `runtime/memory/` for memory and other transient state).

**In CLAUDE.md:** nothing.

**Codex equivalent:** none needed.

---

## (6) Step records as the TodoWrite replacement

**Original quote** (line 23):
> - **Step records** (`tk create --step "..."`) are the replacement for `TodoWrite`: turn-bound, creator-private progress markers that render as nodes on the user-facing chat progress view (a vertical timeline with a status icon and a one-line summary per step). Most turns use only these.

**In AGENTS.md:**
> - **Step records** (`tk create --step "..."`) are the go-to tool to write a todo: turn-bound, creator-private progress markers that render as nodes on the user-facing chat progress view (a vertical timeline with a status icon and a one-line summary per step). Most turns use only these.

**In CLAUDE.md:**
> Step records (`tk create --step "..."`) are the replacement for `TodoWrite`.

**Codex equivalent:** TBD -- same open question as (1); if `update_plan` is live, the codex
line would name it the same way ("step records are the replacement for `update_plan`").

---

## (7) Bash tool timeout

**Original quote** (line 117):
> - When running pytest with a Bash tool timeout, always set `PYTEST_MAX_DURATION_SECONDS` to match the timeout (in seconds). [...]

**In AGENTS.md:** the harness's shell tool is named by example rather than dropped, so each
harness recognizes its own --
> - When running pytest with a shell-command timeout (Claude Code's Bash tool, Codex's exec tool, or your harness's equivalent), always set `PYTEST_MAX_DURATION_SECONDS` to match the timeout (in seconds). [...]

**In CLAUDE.md:** nothing.

**Codex equivalent:** already handled inline in AGENTS.md by naming codex's exec tool.
Confirm the exact tool name codex sees.

---

## Summary

| # | Claudism | AGENTS.md | CLAUDE.md | Codex line needed? |
|---|---|---|---|---|
| 1 | `TodoWrite` disabled | neutralized | restated + "use tk instead" | TBD (`update_plan`) |
| 2 | `CLAUDE.md` self-edit | -> `AGENTS.md` | nothing | TBD (pointer to `.codex/AGENTS.md`) |
| 3 | `.claude/skills` symlink | deleted | symlink noted | no |
| 4 | memory system | moved out (github-sync kept) | full paragraph | **yes** (no auto-memory) |
| 5 | "for Claude memory" | word dropped | nothing | no |
| 6 | step records = `TodoWrite` replacement | neutralized | restated | TBD (same as 1) |
| 7 | Bash tool timeout | names each harness's tool | nothing | handled inline |

**Ctrl-F terms** to find every occurrence in the original `CLAUDE.md`: `Claude`, `TodoWrite`,
`Bash tool`, `.claude`.
