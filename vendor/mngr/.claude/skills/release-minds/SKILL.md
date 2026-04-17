---
name: release-minds
argument-hint: <version-tag>
description: Cut a new "production" release of the minds app. Pushes a release branch in the mngr clone at ~/project/minds_prod, syncs vendor/mngr in ~/project/forever-claude-template to match, and pushes the same-named branch there. Use when the user asks to "release a new version of minds", "cut a minds release", "update the vendored mngr in forever-claude-template to track <branch>", or anything of that shape.
---

# Release a new version of the minds app

The user keeps a "production" clone of mngr at `~/project/minds_prod` and a consumer repo at `~/project/forever-claude-template` whose `vendor/mngr/` directory is a checked-in copy of mngr. A "release" means: pick a branch name in `minds_prod`, publish it, and make the forever-claude-template's vendored copy point at exactly that commit -- with a matching branch in forever-claude-template so the two stay in sync.

## Inputs

- `$1` (required): the release branch name, e.g. `minds_v0.1.1`. This must be the branch the user intends to ship. If the user did not provide one, ask which branch before doing anything.

## Preconditions to check (fail loudly if any are wrong)

1. `~/project/minds_prod` exists and is a git checkout whose `origin` points at `git@github.com:imbue-ai/mngr.git`.
2. `~/project/forever-claude-template` exists and has `origin` pointing at `git@github.com:imbue-ai/forever-claude-template.git` and a remote (typically `mngr`) that points at the mngr repo.
3. Both checkouts have clean working trees (`git status --porcelain` empty). Do not start a release on top of uncommitted work -- surface the dirty state to the user and stop.
4. The current branch in `~/project/minds_prod` is `$1`. If not, ask the user before switching -- they may have intended a different checkout. Never force-switch.
5. The user has network access to push (the skill assumes SSH keys are configured; if a push fails with auth, surface the real error rather than retrying).

## Steps

### 1. Push the mngr release branch

In `~/project/minds_prod`:

```bash
git push -u origin "$1"
```

If the branch already exists on the remote at the same SHA, this is a no-op and fine. If it exists at a different SHA, **stop** and ask the user before overwriting -- a production release branch should not silently move.

Record `HEAD` SHA: `git rev-parse HEAD`. Use the full SHA in the commit message below; use the short SHA in conversational references.

### 2. Create/switch to the matching branch in forever-claude-template

In `~/project/forever-claude-template`:

- If a local branch `$1` already exists, `git checkout $1`.
- Else if `origin/$1` exists, `git checkout -b $1 origin/$1`.
- Else `git checkout -b $1` (branching from whatever is checked out -- usually `main`; confirm with the user if the current branch is unexpected).

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

### 5. Push the forever-claude-template branch

```bash
cd ~/project/forever-claude-template && git push -u origin "$1"
```

Same guard as step 1: if `origin/$1` exists at a different SHA, stop and confirm before force-pushing.

### 6. Report

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

The release has four mutating actions: two pushes and a commit in the middle. If you've already pushed the mngr branch but the forever-claude-template commit/push fails, that's recoverable -- the mngr branch on origin is authoritative, and re-running the skill from step 2 will reconverge. Surface the partial state to the user; do not silently retry.
