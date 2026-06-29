---
name: assist
description: Diagnose and fix a problem the user is hitting in this workspace, and escalate built-in (non-user) issues to imbue. Invoked as `/assist <description>` by the minds "get help -> have an agent help" flow (also usable directly when the user describes something broken).
---

# Assisting with a problem

You were launched (usually via `/assist <description>`) to help the user with something that is going wrong in this workspace. Your job: understand the problem, fix what you can, and -- when the problem is in built-in (not user-written) code -- report it to imbue so they can fix it upstream.

Work the steps below in order. Keep the user informed in plain language as you go.

## 1. Understand the problem

- Read the user's description carefully. If it is vague, state your assumptions and proceed; ask only if you truly cannot start.
- Reproduce or locate the failure. Gather evidence rather than guessing:
  - Service logs: `supervisorctl status` and `/var/log/supervisor/<name>-stdout.log` / `-stderr.log`.
  - The relevant app/service code and any error/traceback you can find.
  - Recent changes: `git log --oneline -20`.
- If you cannot reproduce or find any evidence, say so honestly before going further.

## 2. Find the root cause

Trace the actual failing code path to the specific file and line(s) responsible. Do not pattern-match from symptoms -- find the real cause.

## 3. Classify the cause

Two independent questions decide what to do next.

### A. Is it user-created code or built-in code? (decides whether to escalate)

Classify by git history:

- **Built-in** if the file is any of:
  - under `vendor/` (a vendored snapshot of an external repo, e.g. `vendor/mngr/`), or
  - introduced by the initial template commit (the root commit of this repo's history), or
  - last changed by a commit reachable from an `update-self:` merge (the `/update-self` skill merges upstream template code with a commit subject starting `update-self:`).
- **User-created** otherwise (you or the user wrote it in this workspace).

Useful checks:

```bash
# Who last touched the file, and what was that commit?
git log -1 --format="%H %s" -- <path>
# Is <path> under a vendored tree?
case "<path>" in vendor/*) echo built-in-vendor ;; esac
# List the update-self merges (built-in code arrived through these).
git log --grep="^update-self:" --oneline
```

### B. Which layer is it in? (decides whether you *can* fix it)

- **Fixable from here** -- you run inside this container, so you can change and immediately use:
  - user code,
  - template built-in code (e.g. `apps/system_interface`, skills, scripts), and
  - `vendor/mngr/` code, **but only** when the fix changes how mngr behaves *inside this container* (the container runs from this checkout).
- **Not fixable from here (give up on the fix)** -- the fix would require a new build of the outer minds desktop app, which you cannot produce. This is anything in the *installed outer app*:
  - `vendor/mngr/apps/minds` (the desktop app itself),
  - the outer app's bundled plugins `vendor/mngr/libs/mngr_forward` and `vendor/mngr/libs/mngr_latchkey`,
  - the outer app's own vendored mngr.

  You can still read all of these under `vendor/mngr/` to diagnose and to write a precise report -- you just cannot deploy a fix.

## 4. Fix what you can

If the issue is fixable from here (per B), fix it: make the change, verify it actually resolves the problem (run it, don't just assume), and commit. Tell the user what you changed.

If it is not fixable from here, do not fake a fix. Explain that it needs a new version of the desktop app and that you are reporting it to imbue.

## 5. Report built-in issues to imbue

Report **any built-in-code issue** (per A) to imbue -- even if you already fixed it (the upstream copy still needs the fix). Do **not** report purely user-created issues; those are yours and the user's to handle.

To report, do **not** submit directly. POST your diagnosis to the minds report route through the latchkey gateway. The desktop app then opens a pre-filled "report a bug" modal for the user to review and submit (the human gates the send):

```bash
DESCRIPTION="$(cat <<'EOF'
<one-paragraph summary of the problem>

Root cause: <file:line and what is wrong>
Classification: built-in (<vendor / template / update-self>), <fixable here | needs a new desktop-app version>
Fix: <what you changed, or why it cannot be fixed from here>
EOF
)"

latchkey curl -sS -X POST \
  "http://latchkey-self.invalid/minds-api-proxy/api/v1/agents/$MNGR_AGENT_ID/report" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg d "$DESCRIPTION" '{description: $d}')"
```

A successful call returns `{"ok": true}` and pops the pre-filled modal in the app. Tell the user a report has been opened for their review.

## Summary of decisions

| Cause is...                                  | Fix it?                | Report to imbue? |
|----------------------------------------------|------------------------|------------------|
| User-created code                            | Yes                    | No               |
| Template built-in (system_interface, skills) | Yes                    | Yes              |
| `vendor/mngr` affecting this container        | Yes                    | Yes              |
| Outer app (`apps/minds`, `mngr_forward`, `mngr_latchkey`, outer vendored mngr) | No -- needs a new app build | Yes |
