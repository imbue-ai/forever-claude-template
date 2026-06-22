---
name: submit-upstream-changes
description: Push local improvements to shared infrastructure (skills, scripts, CLAUDE.md scaffolding, Dockerfile, supervisord.conf) back to the parent template repo so other agents derived from the template benefit. Opens a separate per-feature PR per logical fix; never pushes directly to upstream `main`. Do not push agent-specific content (PURPOSE.md, memory, runtime state). For pulling updates from upstream, use the `update-self` skill instead.
---

# Pushing changes upstream

This repo was created from a parent template repo (see `parent.toml` for the upstream URL and branch). The default flow for pushing improvements back is: **one logical fix per PR, on a `submit/<short-name>` branch**. We do not push directly to upstream `main`.

## What to push (and what not to)

Push **shared infrastructure** that benefits other agents derived from the template:

- Skills (`.agents/skills/`)
- Scripts (`scripts/`, `.agents/shared/scripts/`)
- CLAUDE.md scaffolding (template-level sections only)
- Dockerfile
- `supervisord.conf` (template-level service programs)

Do **not** push agent-specific content:

- `PURPOSE.md`
- Memory contents
- Runtime state (`runtime/`)
- Agent-specific services, settings, or CLAUDE.md sections

## PR conventions

- **Branch name:** `submit/<short-feature-name>` (kebab-case, ~3-5 words). Same name on the upstream remote.
- **One logical fix per PR.** Multiple commits are fine if they form one logical unit; otherwise split them across PRs so each can be reviewed/CI'd/merged independently.
- **Title:** short, imperative, scoped. e.g. `forwarder: redirect HTTPS by default`.
- **Body:** a single paragraph explaining the *why* (the motivating bug, missing capability, or constraint). Reviewers can read the diff for the *what*. Skip checklists and section headers.
- **Co-Authored-By trailer** on the commit (the standard one used in this repo).

## Recipe

The upstream URL and base branch are in `parent.toml`.

1. Ensure the `upstream` remote points at the template (idempotent):

   ```bash
   git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
   import tomllib
   with open('parent.toml', 'rb') as f:
       print(tomllib.load(f)['url'])
   ")"
   ```

2. Stage the commit(s) you want to push onto a clean throwaway branch rooted at upstream's base, then push that branch. Pushing the local working branch directly (`git push upstream <local_branch>:submit/<short-name>`) would publish every ancestor commit not yet on upstream -- including unrelated WIP, merge, and scaffolding commits that happen to share the branch tip's history -- producing a noisy PR that violates the "one logical fix per PR" rule.

   **If your working tree is dirty** (`git status` shows modifications you don't want to disturb), do the cherry-pick in a fresh `git worktree` rooted at `upstream/<base>`. Switching branches in-place would either carry the dirty tracked changes into the submit branch (and into the cherry-pick context, where they cause conflicts) or refuse the checkout outright. A worktree sidesteps both:

   ```bash
   git fetch upstream
   BASE=$(python3 -c "
   import tomllib
   with open('parent.toml', 'rb') as f:
       print(tomllib.load(f)['branch'])
   ")
   git worktree add /tmp/wt-<short-name> "upstream/$BASE"
   (cd /tmp/wt-<short-name> \
        && git checkout -b submit/<short-name> \
        && git cherry-pick <sha-1> [<sha-2> ...] \
        && git push upstream submit/<short-name>:submit/<short-name>)
   git worktree remove /tmp/wt-<short-name>
   git branch -D submit/<short-name>   # the throwaway local branch
   ```

   With a clean working tree, the simpler in-place form is fine:

   ```bash
   git fetch upstream
   BASE=$(python3 -c "
   import tomllib
   with open('parent.toml', 'rb') as f:
       print(tomllib.load(f)['branch'])
   ")
   git branch -f submit/<short-name> "upstream/$BASE"
   git checkout submit/<short-name>
   git cherry-pick <sha-1> [<sha-2> ...]   # the commit(s) for this logical fix, oldest first
   git push upstream submit/<short-name>:submit/<short-name>
   git checkout -   # back to your working branch
   ```

   If the cherry-pick conflicts against current upstream (a real conflict, not the dirty-tree case above), resolve it the same way you would for any cherry-pick (or rebase your fix on a fresh `update-self` first).

3. Open the PR against the template's default branch (read from `parent.toml`, usually `main`):

   ```bash
   gh pr create \
       --repo imbue-ai/forever-claude-template \
       --base main \
       --head submit/<short-name> \
       --title "<short imperative title>" \
       --body  "<one-paragraph why>"
   ```

4. Report the PR URL back to the user.

## When to push

- When the user asks you to push changes upstream.
- After improving shared skills, scripts, or configuration that would benefit other agents.

## Important

- Always commit your local changes before pushing.
- Double-check the diff: `git show <sha>` -- make sure no agent-specific content is in the commit.
- One upstream PR per logical fix. Don't bundle.
- When finalizing a worker's branch, cherry-pick only the substantive commits -- skip scaffolding, WIP, and auto-generated commits that don't belong upstream.
- Never push directly to upstream `main`.
- To pull updates from upstream, use the `update-self` skill.
