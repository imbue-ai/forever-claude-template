---
name: release-minds
argument-hint: <version-tag>
description: Cut a new "production" release of the minds app. Pushes a release branch in the mngr clone at ~/project/minds_prod, syncs vendor/mngr in ~/project/forever-claude-template to match, and pushes the same-named branch there. Use when the user asks to "release a new version of minds", "cut a minds release", "update the vendored mngr in forever-claude-template to track <branch>", or anything of that shape.
---

# Release a new version of the minds app

The user keeps a "production" clone of mngr at `~/project/minds_prod` and a consumer repo at `~/project/forever-claude-template` whose `vendor/mngr/` directory is a checked-in copy of mngr. A "release" means: pick a branch name in `minds_prod`, publish it, make the forever-claude-template's vendored copy point at exactly that commit on a matching branch, and fast-forward / merge that release branch into forever-claude-template's `main` so downstream clones (made with `new-forever-claude-clone` or any plain `git clone` of FCT) see the released vendor/mngr. Without that final merge, `main` falls behind and fresh clones get a stale vendor that fails to build inside Docker with confusing `uv` errors about conflicting URLs for `imbue-mngr`.

## Inputs

- `$1` (required): the release branch name, e.g. `minds_v0.1.1`. This must be the branch the user intends to ship. If the user did not provide one, ask which branch before doing anything.

## Preconditions to check (fail loudly if any are wrong)

1. `~/project/minds_prod` exists and is a git checkout whose `origin` points at `git@github.com:imbue-ai/mngr.git`.
2. `~/project/forever-claude-template` exists and has `origin` pointing at `git@github.com:imbue-ai/forever-claude-template.git` and a remote (typically `mngr`) that points at the mngr repo.
3. Both checkouts have clean working trees (`git status --porcelain` empty). Do not start a release on top of uncommitted work -- surface the dirty state to the user and stop.
4. The current branch in `~/project/minds_prod` is `$1`. If not, ask the user before switching -- they may have intended a different checkout. Never force-switch.
5. `~/project/forever-claude-template` is currently on `main` (`git branch --show-current` == `main`) and `main` is up to date with `origin/main` (`git fetch origin main && git status -sb` shows no "behind"). Each release must be cut from a fresh `main` so the release branch captures only the vendor sync, not stray unmerged work from a previous branch. If the checkout is on another branch or has local/unpushed commits on `main`, stop and ask the user before switching or resetting.
6. The user has network access to push (the skill assumes SSH keys are configured; if a push fails with auth, surface the real error rather than retrying).

## Steps

### 1. Push the mngr release branch

In `~/project/minds_prod`:

```bash
git push -u origin "$1"
```

If the branch already exists on the remote at the same SHA, this is a no-op and fine. If it exists at a different SHA, **stop** and ask the user before overwriting -- a production release branch should not silently move.

Record `HEAD` SHA: `git rev-parse HEAD`. Use the full SHA in the commit message below; use the short SHA in conversational references.

### 2. Create the matching branch in forever-claude-template from `main`

In `~/project/forever-claude-template` (which precondition 5 has already verified is sitting on an up-to-date `main`):

```bash
git checkout -b "$1"
```

If a local branch `$1` already exists, **stop** and ask the user -- either this release was already started (in which case we should not silently re-run) or the branch name collides with unrelated work. Do not `-B` or delete the existing branch.

If `origin/$1` exists but there is no local branch, also stop and ask -- the upstream is authoritative and you should not clobber it without confirmation.

### 3. Replace `vendor/mngr/` contents with the mngr HEAD

Use `git archive` from `minds_prod` -- this gives exactly the tracked files at HEAD with no `.git`, no `.venv`, no caches:

```bash
cd ~/project/minds_prod && git archive --format=tar HEAD > /tmp/mngr_sync.tar
cd ~/project/forever-claude-template/vendor/mngr
rm -rf ./* ./.[!.]*        # clear existing contents, including dotfiles, but keep the directory
tar -xf /tmp/mngr_sync.tar
rm /tmp/mngr_sync.tar
```

Do NOT use `rsync ... --delete` from a live mngr working tree for the release flow -- that would sweep in untracked files (`.venv`, editor swapfiles, etc.). `git archive` is the right tool because it's exactly-the-tracked-tree at the committed SHA.

### 4. Commit the sync

In `~/project/forever-claude-template`:

```bash
git add -A vendor/mngr/
git commit -m "Sync vendor/mngr to $1 (<short-sha>)"
```

Include the full SHA in the body so the commit is self-describing. Example body: "Tracks the `$1` release branch of mngr at commit `<full-sha>`."

**Pre-commit gotcha**: the pre-commit hook in forever-claude-template is generated and references an absolute path under `~/.cache/uv/archive-v0/...` that can go stale. If `git commit` fails with `'pre-commit' not found. Did you forget to activate your virtualenv?`, run:

```bash
uv tool install pre-commit
(cd ~/project/forever-claude-template && uv tool run pre-commit install)
```

and retry the commit. Do not use `--no-verify` to work around this.

### 5. Push the forever-claude-template release branch

```bash
cd ~/project/forever-claude-template && git push -u origin "$1"
```

Same guard as step 1: if `origin/$1` exists at a different SHA, stop and confirm before force-pushing.

### 6. Merge the release branch into forever-claude-template `main` and push

The release is not complete until `main` points at it. Downstream consumers (private clones made with the `new-forever-claude-clone` skill, any fresh `git clone` of FCT) use `main` as their starting point, so leaving `main` behind a release means those clones get a stale `vendor/mngr` and Docker builds may fail with `uv` resolution errors about conflicting URLs for `imbue-mngr`.

```bash
cd ~/project/forever-claude-template
git checkout main
git pull --ff-only origin main
git merge --no-ff "$1" -m "Merge $1 release branch into main"
git push origin main
```

Use `--no-ff` so there's an explicit merge commit marking each release; that makes it easy to skim `git log main` and see where each release landed. If the pull turns up unrelated work on `origin/main` that wasn't on this release branch, stop and ask the user before merging -- the release branch should be ahead of `main`, not diverged from it.

### 7. Report

Report back with, at minimum:
- The mngr SHA that was released.
- The forever-claude-template commit SHA that captured the sync.
- Both branch names (they should be identical -- `$1`).
- Links are nice but don't fabricate URLs; the user knows where the repos live.

## Things not to do

- Do not amend existing commits in either repo. Always a new commit (per the user's repo-wide rule).
- Do not open PRs automatically. The user treats these release branches as long-lived pointers, not as PR sources, so leave them as plain branches unless asked.
- Do not run `uv sync`, `just test-offload`, or any verification in this skill -- the release is a sync-only operation. If the user wants tests, they will ask.
- Do not touch `~/.external_worktrees/forever-claude-template` or any worktree under minds_prod. This skill operates on the two primary checkouts only.
- Do not modify `pyproject.toml`, `uv.lock`, or anything outside `vendor/mngr/` in forever-claude-template. The sync is purely a content replacement of that directory.

## If something goes wrong mid-flight

The release has several mutating actions: a push to the mngr remote, a commit and push of the release branch in forever-claude-template, and then a merge + push to forever-claude-template `main`. If you've already completed early steps but a later one fails, do not silently retry -- surface the partial state to the user. Recovery rules of thumb:
- mngr release branch pushed but FCT sync failed: mngr is authoritative; re-run from step 2.
- FCT release branch pushed but the merge-to-main failed: the release branch on origin is authoritative. Re-running from step 6 (checkout main, merge the release branch, push) is safe and idempotent.
- The merge-to-main produced an unexpected conflict: stop. A clean release should always be a straightforward merge of FCT's release branch into FCT's `main` when main hasn't diverged. A conflict signals unrelated work on main; ask the user how to proceed rather than resolving it autonomously.
