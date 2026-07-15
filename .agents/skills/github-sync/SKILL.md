---
name: github-sync
description: Enable, check, or disable GitHub sync for this workspace. Enabling creates a dedicated PRIVATE GitHub repo via latchkey, points origin at it, auto-pushes every commit from every checkout, and continuously syncs runtime/ state (memory, tickets, transcripts). Use when the user asks to back up / sync the workspace to GitHub, enable auto-push, restore a previous workspace's state, or asks about GitHub sync status.
compatibility: Requires latchkey (see the latchkey skill) and the user approving GitHub permissions in the Minds app.
---

# GitHub sync

GitHub sync is opt-in. Nothing syncs until this skill enables it. Once
enabled, three pieces work together (see `libs/github_sync/README.md`):

1. `origin` points at a dedicated **private** GitHub repo for this workspace.
2. Global git wiring routes all `https://github.com/...` traffic through the
   latchkey gateway (credential injected server-side; no token in the
   container) and activates the `post-commit` hook, so every commit on any
   checkout -- main repo and worker worktrees -- auto-pushes its branch.
3. The `[program:github-sync]` service commits + pushes `runtime/` to the
   `runtime-sync` orphan branch every 60s and re-verifies the repo stays
   private, halting pushes if it ever isn't.

## Hard rules

- **Private repos only.** Never create a public repo, never point sync at a
  public repo, and never work around a visibility halt. If the user asks for
  a public sync repo, decline and explain: agents can push secrets or other
  sensitive data without realizing it.
- **Everything flows through latchkey.** Never ask the user for a GitHub
  token and never embed credentials in URLs or git config.
- `origin` is reserved for the sync repo. Upstream-template operations keep
  using `parent.toml` (see the update-self skill) and are unaffected.

## Enable

1. **Check current state**: `uv run github-sync status`. If `is_configured`
   is already true, jump to "Status" (or "Repair" if the service is
   unhealthy). Also run `supervisorctl status github-sync` (it errors when no
   such program exists -- expected before enable).

2. **Request GitHub permissions** through latchkey (see the latchkey skill
   for the permission-request mechanics). GitHub exposes two latchkey scopes
   and a permission request carries exactly one scope, so this is two
   requests -- **fire both back-to-back, before any other GitHub call**, so
   the user approves them in a single sitting. Never dribble out further
   requests later in the flow.

   ```bash
   latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
     -H 'Content-Type: application/json' \
     -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "predefined", "payload": {"scope": "github-git", "permissions": ["github-git-read", "github-git-write"]}, "rationale": "GitHub sync: push this workspace'"'"'s branches and runtime state to your private sync repo."}'
   latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
     -H 'Content-Type: application/json' \
     -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "predefined", "payload": {"scope": "github-rest-api", "permissions": ["github-read-user", "github-read-repos", "github-write-all"]}, "rationale": "GitHub sync: create the private sync repo (needs github-write-all), confirm which GitHub account it lands under (github-read-user), and verify it stays private (github-read-repos)."}'
   ```

   This exact permission set is what the flow needs -- do not trim it, or the
   user gets asked again mid-flow:

   - `github-write-all` -- repo creation (`POST /user/repos`). The narrower
     `github-write-repos` covers only existing-repo (`/repos/{owner}/{repo}`)
     paths and is **not** enough to create a repo. It also covers the
     optional repo deletion on disable.
   - `github-read-repos` -- the recurring private-visibility check
     (`GET /repos/{owner}/{repo}`), which the service repeats forever.
   - `github-read-user` -- `GET /user`, to name the account the repo will be
     created under (step 3) and as the one legitimate "did the grants land?"
     probe (below).
   - `github-git-read` / `github-git-write` -- clone/fetch and push.

   Then **wait for both approval system messages** ("Your permission request
   for GitHub (git) / (REST API) was granted..."). Those messages are the
   authoritative signal; they are what tells you to proceed.

   **Do not treat a rejected API call as evidence that a grant is missing.**
   `{"error": "Error: Request not permitted by the user."}` means *that
   endpoint* is not covered by the granted permissions -- it does not mean
   the approval failed to arrive. Probing an endpoint outside the set above
   (e.g. `GET /user/repos`, which needs broader read access) will be rejected
   even when everything is granted correctly. If you want to sanity-check
   after the grant messages arrive, the only probe to use is:

   ```bash
   latchkey curl -s https://api.github.com/user
   ```

   Never re-ask the user for a permission you have already been told was
   granted; re-read this list and check *which endpoint* you called instead.

3. **Pick the repo.** Default: create a brand-new private repo named after
   the workspace (`$MINDS_WORKSPACE_NAME`), owned by the authenticated GitHub
   account (read its `login` from `latchkey curl -s https://api.github.com/user`).
   Confirm the name and owner with the user first; they can name an org
   instead. One exception: if `git remote get-url origin` already points at a
   user-owned repo (not `imbue-ai/default-workspace-template` or another
   shared template), ask whether to reuse it or create a fresh one --
   recommend a fresh dedicated repo unless they have a specific reason.
   Reused repos must be verified private and writable like new ones.

4. **Create the repo** (skip if reusing):

   ```bash
   latchkey curl -s -X POST https://api.github.com/user/repos \
     -H 'Content-Type: application/json' \
     -d '{"name": "<repo-name>", "private": true, "description": "Private sync repo for the <workspace> minds workspace"}'
   ```

   (For an org: `POST https://api.github.com/orgs/<org>/repos`.) On a 422
   name-taken error, append `-2`, `-3`, ... and retry. The response JSON must
   contain `"private": true` -- if it does not, delete/abandon the repo and
   stop; do not proceed with a public repo. The response's `full_name` is the
   authoritative `<owner>/<repo>` to use from here on.

5. **Point origin at it and record the config**:

   ```bash
   git remote set-url origin https://github.com/<owner>/<repo>.git \
     || git remote add origin https://github.com/<owner>/<repo>.git
   ```

   Write `github_sync.toml` at the repo root (this file is the "sync is
   enabled" marker for the service and the post-commit hook):

   ```toml
   # Written by the github-sync skill. Presence of this file enables GitHub
   # sync; `uv run github-sync status` reports on it.
   repo_url = "https://github.com/<owner>/<repo>"
   ```

6. **Wire git through the gateway**: `uv run github-sync wire-git`. From now
   on plain `git push`/`git fetch` against github.com works in every
   checkout, and the post-commit auto-push hook is active.

7. **Create (or restore) the runtime worktree**:
   `uv run github-sync setup-worktree`. If origin already has a
   `runtime-sync` branch (re-enabling for a workspace recreated from a
   previously-synced repo), this restores the prior runtime/ state instead of
   starting fresh -- tell the user their memory/tickets/transcripts are back.

8. **Verify private before any push**: `uv run github-sync check-visibility`
   must print `private` (exit 0). If not, stop and surface the problem.

9. **Initial sync**: push the current branch, the runtime-sync branch, and
   any existing worker branches:

   ```bash
   git push --set-upstream origin "$(git branch --show-current)"
   git -C runtime push --set-upstream origin runtime-sync
   for b in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v -x -e "$(git branch --show-current)" -e runtime-sync); do git push origin "$b"; done
   ```

10. **Add the service** by appending this block to `supervisord.conf`, then
    `supervisorctl reread && supervisorctl update` (see the edit-services
    skill):

    ```ini
    # Opt-in GitHub sync (added by the github-sync skill): commits + pushes
    # runtime/ to the runtime-sync branch of the private sync repo and
    # re-verifies the repo stays private. See libs/github_sync/README.md.
    # The oom_tag_service.py prefix sets its OOM shed-priority band (see
    # libs/oom_priority): a runtime-state backup is shed after the UI/tunnel/
    # terminal but before host-backup, matching what runtime-backup used to get.
    [program:github-sync]
    command=python3 scripts/oom_tag_service.py github-sync uv run github-sync run
    directory=/mngr/code
    autostart=true
    autorestart=true
    startretries=1000000
    stopasgroup=true
    killasgroup=true
    stdout_logfile=/var/log/supervisor/github-sync-stdout.log
    stderr_logfile=/var/log/supervisor/github-sync-stderr.log
    stdout_logfile_maxbytes=10MB
    stderr_logfile_maxbytes=10MB
    stdout_logfile_backups=3
    stderr_logfile_backups=3
    ```

11. **Commit the enablement** (`github_sync.toml` + `supervisord.conf`). The
    now-active hook pushes the commit; this also makes sync sticky if the
    repo is later used to recreate the workspace.

12. **Report**: the repo URL, that every commit now auto-pushes, that
    runtime/ syncs every minute, and that pushes queue while their machine
    (the latchkey gateway) is offline (on remote hosts the per-VPS secondary
    gateway usually covers that).

## Status

`uv run github-sync status` prints config + the service's latest status
(visibility, last push, errors); `supervisorctl status github-sync` shows the
process; logs are at `/var/log/supervisor/github-sync-*.log` and
`/tmp/github-sync.log`, hook output at `/tmp/post-commit-push.log`. Explain
findings in plain language. If `is_push_allowed` is false, the repo is public
or unverifiable -- tell the user to make it private again; sync resumes
automatically.

## Repair (workspace recreated from a synced repo)

A workspace created from a previously-synced private repo inherits
`github_sync.toml` and the service block, but not the latchkey permissions or
the container-local wiring/worktree. The service idles until it can reach
origin. To repair: run step 2 (permission requests); the service self-heals
within a tick (re-wires git, restores runtime/ from origin). Verify with
"Status", or accelerate with `uv run github-sync wire-git` +
`uv run github-sync setup-worktree`.

## Disable

Confirm with the user first, and ask separately whether to keep the remote
repo (recommend keeping it -- it costs nothing and preserves history).

1. `supervisorctl stop github-sync`, remove the `[program:github-sync]` block
   from `supervisord.conf`, then `supervisorctl reread && supervisorctl update`.
2. `uv run github-sync unwire-git` (removes the gateway git config and the
   hooks path -- auto-push stops).
3. Delete `github_sync.toml`.
4. Leave the local `runtime/` worktree and its history intact (harmless, and
   re-enabling picks it right back up).
5. If the user chose to delete the remote repo:
   `latchkey curl -s -X DELETE https://api.github.com/repos/<owner>/<repo>`
   (covered by the `github-write-all` granted at enable). If the grant has
   since been revoked, do not re-request it just for this -- point them at
   the repo's GitHub settings page to delete it themselves.
6. Commit the removal. Note that this commit is NOT auto-pushed (the hook is
   inert again).
