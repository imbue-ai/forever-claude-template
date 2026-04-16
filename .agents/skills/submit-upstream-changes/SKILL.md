---
name: submit-upstream-changes
description: Push local improvements to shared infrastructure (skills, scripts, AGENTS.md scaffolding, Dockerfile, services.toml) back to the upstream template repo so other agents derived from the template can benefit. Do not push agent-specific content (PURPOSE.md, memory, runtime state). For pulling updates from upstream, use the `update-self` skill instead.
---

# Pushing changes to the upstream template

This repo was created from a template repo. The two share git history and stay connected via a git remote. The template URL and branch are defined in `parent.toml`:

```toml
url = "https://github.com/imbue-ai/forever-claude-template.git"
branch = "main"
```

## What to push (and what not to)

Push changes to **shared infrastructure** that would benefit other agents derived from the template:

- Skills (`.agents/skills/`)
- Scripts (`scripts/`)
- AGENTS.md scaffolding (the template-level sections)
- Dockerfile
- `services.toml` (template-level entries)

Do **not** push agent-specific customizations:

- `PURPOSE.md`
- Memory contents
- Runtime state
- Agent-specific services, settings, or AGENTS.md sections

## Setup

Ensure the `upstream` remote exists:

```bash
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
```

## Pushing changes

```bash
BRANCH=$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['branch'])
")
git push upstream HEAD:"$BRANCH"
```

If you need to push to a different branch on the template (e.g., a feature branch), replace `"$BRANCH"` with the target branch name.

## When to push

- When the user asks you to push changes upstream
- After improving shared skills, scripts, or configuration that would benefit other agents

## Important

- Always commit your local changes before pushing
- Double-check the diff — make sure you're not about to push agent-specific content
- To pull updates from upstream, use the `update-self` skill
