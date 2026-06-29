---
name: assist
description: Diagnose and fix a problem the user is hitting in this workspace, and escalate built-in (non-user) issues to imbue. Invoked as `/assist <description>` by the minds "get help -> have an agent help" flow (also usable directly when the user describes something broken).
---

# Assisting with a problem

You were launched (usually via `/assist <description>`) to help the user with something that is going wrong in this workspace. Your job: understand the problem, confirm your diagnosis with the user before touching anything, fix what you can, and -- when the problem is in built-in (not user-written) code -- report it to imbue so they can fix it upstream.

Work the steps below in order. Keep the user informed in plain language as you go.

## 1. Understand the problem

- Read the user's description carefully. If it is vague, state your assumptions and proceed; ask only if you truly cannot start.
- **Reproduce or directly observe the failure before theorizing about it.** Actually trigger the thing the user reported and watch it happen -- measure the slow operation, hit the failing endpoint, reproduce the error. Gather evidence rather than guessing:
  - Service logs: `supervisorctl status` and `/var/log/supervisor/<name>-stdout.log` / `-stderr.log`.
  - The relevant app/service code and any error/traceback you can find.
  - Recent changes: `git log --oneline -20`.
- If you cannot reproduce or find any evidence, say so honestly before going further -- do not paper over the gap with a plausible-sounding theory.

## 2. Find the root cause

Trace the actual failing code path to the specific file and line(s) responsible. Do not pattern-match from symptoms -- find the real cause.

**Match your confidence to your evidence.** A code path you found by reading is a *hypothesis* until you have observed it actually causing the reported symptom. Do not announce a cause as "confirmed" on the strength of code-reading alone -- confirm it by reproducing the symptom and tying it to that code (e.g. the slow operation measurably speeds up when you bypass the suspected hot path; the error stops when you correct the suspected line). Until then, describe it as your leading suspicion, not a settled fact.

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

- **Fixable from here** -- you run inside this container, so you can change and use (*how* you apply each fix safely differs by artifact -- see step 5):
  - user code,
  - template built-in code (e.g. `apps/system_interface`, skills, scripts), and
  - `vendor/mngr/` code, **but only** when the fix changes how mngr behaves *inside this container* (the container runs from this checkout).
- **Not fixable from here (give up on the fix)** -- the fix would require a new build of the outer minds desktop app, which you cannot produce. This is anything in the *installed outer app*:
  - `vendor/mngr/apps/minds` (the desktop app itself),
  - the outer app's bundled plugins `vendor/mngr/libs/mngr_forward` and `vendor/mngr/libs/mngr_latchkey`,
  - the outer app's own vendored mngr.

  You can still read all of these under `vendor/mngr/` to diagnose and to write a precise report -- you just cannot deploy a fix.

## 4. Confirm the diagnosis and plan before you change anything

You are editing in the **same work directory the user's live workspace is served from** -- this `/assist` chat is a `chat`-type agent that shares that checkout, not an isolated clone. So before you touch any code, check in with the user:

- State the cause you found, the evidence that backs it, and the exact change you propose to make.
- Wait for their go-ahead. Do not start editing on your own authority, even when you are confident -- the user asked you to help diagnose a problem, not to perform an unrequested rewrite.

(For a system-interface fix, this verbal go-ahead covers the *plan*; the `update-system-interface` preview in step 5 is where the user approves the actual change before it goes live.)

## 5. Fix what you can: quick live fix, then defer the hardening

If the issue is **not fixable from here** (per B -- it lives in the installed outer app), do not fake a fix. Explain that it needs a new version of the desktop app, and go report it (step 6).

If it **is fixable from here**, unblock the user fast, then harden in the background. *How* you apply the fix depends on the artifact:

- **`apps/system_interface`** (the workspace UI: dockview shell, chat panels, progress view): **never edit it directly here.** Because your checkout is the one being served, a hand-edit-and-rebuild can take the user's entire UI down with no surface left to show an error. Route the fix through the **`update-system-interface`** skill, whose preview lets the user approve the change and whose reveal step pre-flights on a throwaway port and auto-rolls-back on failure -- the only safe go-live for the UI. Since `/assist` shares the work dir and can spawn the worker, you drive that flow yourself.
- **A skill, or a web service whose code is broken:** make the quick fix live so the user is unblocked now, then at turn-end defer the hardening (tests, review gates, isolated verification) to the **`heal-artifact`** skill rather than treating your inline edit as the finished article. Use **`edit-services`** instead if the fix is to add/remove/reconfigure a service rather than repair its code.
- **User-written code:** make the fix live and verify it actually resolves the problem (run it -- don't assume). This is the user's own code, so there is no lifecycle skill to defer to; just tell them what you changed.

Whatever the path, verify that the symptom you reproduced in step 1 is actually gone before you call it fixed, and tell the user plainly what changed.

## 6. Report built-in issues to imbue

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

Always confirm the diagnosis and plan with the user (step 4) before applying any of these.

| Cause is...                                   | How to fix it                                                                 | Report to imbue? |
|-----------------------------------------------|-------------------------------------------------------------------------------|------------------|
| User-created code                             | Fix live, verify it works                                                     | No               |
| Template built-in: `apps/system_interface`    | Route through `update-system-interface` (never edit the served tree directly) | Yes              |
| Template built-in: a skill or web service     | Quick live fix, then defer hardening to `heal-artifact` (`edit-services` for service config) | Yes |
| Other template built-in (scripts, etc.)       | Fix live, verify it works                                                     | Yes              |
| `vendor/mngr` affecting this container         | Fix live, verify it works                                                      | Yes              |
| Outer app (`apps/minds`, `mngr_forward`, `mngr_latchkey`, outer vendored mngr) | Cannot -- needs a new app build              | Yes |
