@AGENTS.md

# Claude

Claude Code's built-in `TodoWrite` is disabled, use tk instead. Step records (`tk create --step "..."`) are the replacement for `TodoWrite`.

`.claude/skills` does not exist, it is symlinked to `.agents/skills/`.

# Memory

Use Claude's built-in memory system. Your memory directory is `runtime/memory/` (configured via `autoMemoryDirectory` in `.claude/settings.json`). Memory is gitignored from the main branch.
