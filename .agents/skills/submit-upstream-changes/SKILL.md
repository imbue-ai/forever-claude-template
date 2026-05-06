---
name: submit-upstream-changes
description: Push local improvements to shared infrastructure (skills, scripts, CLAUDE.md scaffolding, Dockerfile, services.toml, vendor/mngr/) back to their upstream repos so other agents derived from the template benefit. Opens a separate per-feature PR per logical fix; never pushes directly to upstream `main`. Do not push agent-specific content (PURPOSE.md, memory, runtime state). For pulling updates from upstream, use the `update-self` skill instead.
---

# Pushing changes upstream

This repo was created from the `imbue-ai/forever-claude-template` template, and embeds `imbue-ai/mngr` as a git subtree at `vendor/mngr/`. Each upstream is a different GitHub repo and needs a different push recipe. The default flow for either is: **one logical fix per PR, on a `submit/<short-name>` branch**. We do not push directly to upstream `main`.

## What to push (and what not to)

Push **shared infrastructure** that benefits other agents derived from the template:

- Skills (`.agents/skills/`)
- Scripts (`scripts/`, `.agents/shared/scripts/`)
- CLAUDE.md scaffolding (template-level sections only)
- Dockerfile
- `services.toml` (template-level entries)
- `vendor/mngr/...` -- pushed to its own upstream (`imbue-ai/mngr`), not the template

Do **not** push agent-specific content:

- `PURPOSE.md`
- Memory contents
- Runtime state (`runtime/`)
- Agent-specific services, settings, or CLAUDE.md sections

## Pick the upstream target

Look at the paths your commit touches:

- Anything under `vendor/mngr/` -> upstream is **`imbue-ai/mngr`**. Use the **vendor/mngr recipe** below.
- Everything else -> upstream is **`imbue-ai/forever-claude-template`** (URL/branch from `parent.toml`). Use the **template recipe** below.

If a single commit straddles both (it shouldn't, but if it does), split it into two commits before continuing -- each upstream PR must be self-contained.

## Pre-flight: GraphQL rate limit

`gh pr create` and `gh repo view` go through the GraphQL API, which has a per-user 5000/hour quota shared across the org. Mid-session it can already be exhausted, and `gh pr create` then fails with an opaque "API rate limit already exceeded". Check first:

```bash
gh api rate_limit --jq '.resources.graphql | "remaining=\(.remaining) reset=\(.reset)"'
```

If `remaining` is 0 (or single-digit and you have several PRs to open), stop. Format the reset epoch for the user and wait, e.g.:

```bash
date -u -d "@$(gh api rate_limit --jq '.resources.graphql.reset')" \
    +'GraphQL quota resets at %Y-%m-%dT%H:%M:%SZ'
```

Do not retry-loop; surface the reset time and hand back to the user.

## PR conventions

- **Branch name:** `submit/<short-feature-name>` (kebab-case, ~3-5 words). Same name on the upstream remote.
- **One logical fix per PR.** Multiple commits are fine if they form one logical unit; otherwise split them across PRs so each can be reviewed/CI'd/merged independently.
- **Title:** short, imperative, scoped. e.g. `forwarder: redirect HTTPS by default`.
- **Body:** a single paragraph explaining the *why* (the motivating bug, missing capability, or constraint). Reviewers can read the diff for the *what*. Skip checklists and section headers.
- **Co-Authored-By trailer** on the commit (the standard one used in this repo). It carries through `format-patch` to vendor/mngr too.

## Recipe A: template upstream (`imbue-ai/forever-claude-template`)

For changes outside `vendor/mngr/`.

1. Ensure the `upstream` remote points at the template (idempotent):

   ```bash
   git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
   import tomllib
   with open('parent.toml', 'rb') as f:
       print(tomllib.load(f)['url'])
   ")"
   ```

2. Push the local branch's HEAD to a feature branch on upstream (this picks up *the commits on your local branch only* -- if your branch has unrelated upstream-bound commits ahead of the one you mean to push, push from a narrower local ref instead, e.g. `<sha>` or a temporary branch you reset to the right tip):

   ```bash
   git push upstream <local_branch>:submit/<short-name>
   ```

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

## Recipe B: vendor/mngr upstream (`imbue-ai/mngr`)

For changes inside `vendor/mngr/...`. The subtree was added with squash-merge, which means **`git subtree split` does not work** here -- it errors out with `fatal: could not rev-parse split hash <...>` because the squashed merge commits don't carry the original ancestry that `subtree split` needs to reconstruct. Do not try it. Use the clone + `format-patch` + `git am` recipe below instead.

1. Note the commit SHA(s) you want to push, and the relevant branch on `imbue-ai/mngr` (usually `main`):

   ```bash
   SHA=<commit-sha-on-this-branch>
   ```

2. Clone the mngr upstream into a scratch directory, on the right base branch:

   ```bash
   cd /tmp
   rm -rf mngr-pr
   git clone --branch main https://github.com/imbue-ai/mngr.git mngr-pr
   cd mngr-pr
   git checkout -b submit/<short-name>
   ```

3. Generate a patch from this repo, scoped to the subtree path so the diff lands at the right paths in the mngr repo:

   ```bash
   ( cd <path-to-this-repo> && \
     git format-patch -1 "$SHA" --stdout --relative=vendor/mngr/ ) > /tmp/mngr.patch
   ```

   `--relative=vendor/mngr/` strips the `vendor/mngr/` prefix from the patch's paths so they match the mngr repo's layout. If your fix is multiple commits, repeat with `-N` or use a range; one patch file per commit keeps `git am` simple.

4. Apply on the clone:

   ```bash
   git am /tmp/mngr.patch
   ```

   If `git am` fails (whitespace, conflict against a newer mngr `main`), abort with `git am --abort`, rebase your local commit on a fresher subtree pull, and retry.

5. Push and open the PR on the mngr repo:

   ```bash
   git push origin submit/<short-name>
   gh pr create \
       --repo imbue-ai/mngr \
       --base main \
       --head submit/<short-name> \
       --title "<short imperative title>" \
       --body  "<one-paragraph why>"
   ```

6. Report the PR URL back to the user.

## When to push

- When the user asks you to push changes upstream.
- After improving shared skills, scripts, or configuration that would benefit other agents.

## Important

- Always commit your local changes before pushing.
- Double-check the diff per upstream target: `git show <sha>` -- make sure no agent-specific content is in the commit.
- One upstream PR per logical fix. Don't bundle.
- We do **not** push directly to upstream `main`. If you genuinely need to (e.g. a one-off `parent.toml`-driven sync, with explicit user instruction), the existing `parent.toml` lookup still gives you the URL and branch -- but treat it as the rare exception, not the default.
- To pull updates from upstream, use the `update-self` skill.
