---
name: update-self
description: Pull updates from the upstream template repo. Use when you want to get the latest skills, scripts, and configuration improvements.
---

# Updating yourself

Your template repo tracks an upstream source in `parent.toml`:

```toml
url = "https://github.com/imbue-ai/forever-claude-template.git"
branch = "main"
```

The upstream URL may differ from your push remote (e.g., you push to a private fork but pull updates from the public template).

## How to update

1. Add the upstream remote if it doesn't exist:

```bash
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
```

2. Pull the latest changes:

```bash
BRANCH=$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['branch'])
")
git pull upstream "$BRANCH"
```

3. Resolve any merge conflicts if needed, then commit.

## When to update

- When the user asks you to
- When you notice your skills or scripts are outdated
- Periodically, if you're running as a long-lived agent

## Important

- Always commit your local changes before pulling updates
- Review what changed after pulling (use `git log --oneline -10` to see recent commits)
- If a merge conflict occurs in PURPOSE.md or CLAUDE.md, prefer your local version (those are customized per-agent)
