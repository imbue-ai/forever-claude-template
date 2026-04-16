---
name: update-self
description: Pull updates from the upstream template repo. Use when upstream has new skills, script fixes, or config improvements you want to incorporate. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead.
---

# Pulling updates from the upstream template

This repo was created from a template repo. The two share git history and stay connected via a git remote. The template URL and branch are defined in `parent.toml`:

```toml
url = "https://github.com/imbue-ai/forever-claude-template.git"
branch = "main"
```

## What this means

The template contains shared infrastructure: skills, scripts, CLAUDE.md scaffolding, Dockerfile, services.toml, etc. When the template is updated, you can pull those changes in here.

## Setup

Ensure the `upstream` remote exists:

```bash
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
```

## Pulling updates

```bash
BRANCH=$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['branch'])
")
git pull upstream "$BRANCH"
```

Resolve any merge conflicts if needed. For conflicts in files customized per-agent (PURPOSE.md, agent-specific CLAUDE.md sections), prefer your local version.

## When to pull

- When the user asks you to
- When you notice your skills or scripts are outdated
- Periodically, if you're running as a long-lived agent

## Important

- Always commit your local changes before pulling
- Review what changed after pulling (`git log --oneline -10`)
- To push local improvements back upstream, use the `submit-upstream-changes` skill
