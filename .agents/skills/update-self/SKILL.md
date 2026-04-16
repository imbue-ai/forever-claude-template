---
name: update-self
description: Sync with the upstream template repo. Pull when upstream has new skills, script fixes, or config improvements. Push when you've improved shared infrastructure (skills, scripts, CLAUDE.md scaffolding, Dockerfile) that other agents should benefit from. Do not push agent-specific content (PURPOSE.md, memory, runtime state).
---

# Syncing with the upstream template

This repo was created from a template repo. The two share git history and stay connected via a git remote. The template URL and branch are defined in `parent.toml`:

```toml
url = "https://github.com/imbue-ai/forever-claude-template.git"
branch = "main"
```

## What this means

The template contains shared infrastructure: skills, scripts, CLAUDE.md scaffolding, Dockerfile, services.toml, etc. Changes to these files may be useful to all repos derived from the template, so they should be pushed back upstream. Changes specific to this agent instance (e.g., custom PURPOSE.md content, agent-specific services, memory) should stay local.

## Setup

Ensure the `upstream` remote exists:

```bash
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
```

## Pulling updates from the template

Use this when the template has improvements you want (new skills, bug fixes, better config).

```bash
BRANCH=$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['branch'])
")
git pull upstream "$BRANCH"
```

Resolve any merge conflicts if needed. For conflicts in files customized per-agent (PURPOSE.md, agent-specific CLAUDE.md sections), prefer your local version.

## Pushing changes to the template

Use this when you've made improvements to shared infrastructure that other agents should benefit from (e.g., new skills, script fixes, CLAUDE.md improvements).

```bash
BRANCH=$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['branch'])
")
git push upstream HEAD:"$BRANCH"
```

If you need to push to a different branch on the template (e.g., a feature branch), replace `"$BRANCH"` with the target branch name.

## When to pull

- When the user asks you to
- When you notice your skills or scripts are outdated
- Periodically, if you're running as a long-lived agent

## When to push

- When the user asks you to push changes upstream
- After improving shared skills, scripts, or configuration that would benefit other agents

## Important

- Always commit your local changes before pulling or pushing
- Review what changed after pulling (`git log --oneline -10`)
- Do not push agent-specific customizations (PURPOSE.md, memory, runtime state) to the template
