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

The template contains shared infrastructure: skills, scripts, CLAUDE.md scaffolding, Dockerfile, supervisord.conf, etc. When the template is updated, you can pull those changes in here.

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

Always pull with `--no-ff` and the recognizable commit subject below, so the merge that brings in template (built-in) code is identifiable in `git log` afterwards. Tools that classify code as built-in vs. user-created (e.g. the `assist` skill) rely on this `update-self:` subject convention to find which commits came from upstream:

```bash
BRANCH=$(python3 -c "
import tomllib
with open('parent.toml', 'rb') as f:
    print(tomllib.load(f)['branch'])
")
git pull --no-ff --no-edit upstream "$BRANCH" -m "update-self: merge upstream template ($BRANCH)"
```

`--no-ff` forces a real merge commit even when the pull could fast-forward, so the subject is always recorded. Do not amend or reword that subject -- the `update-self:` prefix is the marker.

Resolve any merge conflicts if needed. For conflicts in files customized per-agent (PURPOSE.md, agent-specific CLAUDE.md sections), prefer your local version.

## When to pull

- When the user asks you to
- When you notice your skills or scripts are outdated
- Periodically, if you're running as a long-lived agent

## Important

- Always commit your local changes before pulling
- Review what changed after pulling (`git log --oneline -10`)
- To push local improvements back upstream, use the `submit-upstream-changes` skill
