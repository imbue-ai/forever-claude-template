---
name: use-inspiration
description: Adapt an existing inspiration (a published snapshot of apps/features from another mind) into this mind, filling in its holes interactively. Use when the user gives an inspiration's git URL, or asks to adopt/adapt/reuse a published inspiration.
---

# Adapting an inspiration

Version: v1 (inspirations flow). This versions the publish/adopt flow and the
`inspiration-<slug>.md` manifest format.

An inspiration is a publishable, reusable snapshot of the apps and features a mind
has built. It lives in its own GitHub repo as a real default-workspace-template tree
plus one or more `inspiration-<slug>.md` manifests at the repo root (each with a
sibling `inspiration-<slug>.svg` thumbnail). Adapting an inspiration means bringing
that snapshot into *this* mind and then working through its "holes" — the parts
the original author left stubbed or unwired — together with the user.

All git commands run with cwd = the repo root (`/code`).

## Two entry points

There are two ways this skill starts. Figure out which one applies before doing
anything else.

**A. Template path — this mind was created from an inspiration repo.** The mind
already has the inspiration's tree at its root (it *is* the inspiration repo), so
there is nothing to fetch. On this path adaptation starts IMMEDIATELY at boot:
the published repo ships its own inspiration-specific `/welcome` skill
(generated into the snapshot by the publish flow, replacing the template's
generic welcome), so the booting agent's first response is a custom welcome
naming the inspiration's title and one-line description (instead of the generic
"Welcome to Minds" message), followed in the same turn — without waiting to be
asked — by reading the manifest and asking the user how they want to adapt it.
The manifest's "How to adapt it" section is the script for that conversation.
Default to adapting the **latest** inspiration — the `inspiration-<slug>.md` for
the most-recently-published slug named in that welcome skill. Older
`inspiration-*.md` manifests are reference material and were likely already
adapted by an earlier mind. If more than one manifest is present, you may ask
the user which one they want to adapt. Skip step 1 below (the tree is already
here) and go straight to reading the manifest.

**B. Merge path — the user gave you an inspiration's git URL.** Bring the
inspiration into the *current* mind at the repo root, then adapt it. Do step 1
below to merge it in.

## 0. Trust gate — confirm before merging in (merge path B only)

An inspiration is code published by ANOTHER mind's user, in a repo outside
Imbue's control. **Imbue does not review, verify, or vouch for inspirations.**
Adopting one runs its code in this mind -- its services, skills, and scripts --
and it could contain mistakes or malicious code (data exfiltration, destructive
commands, hidden network calls). You cannot detect that by reading it, so the
only safeguard is the user's informed consent.

On the **merge path (B)**, BEFORE any fetch, merge, or execution in §1, tell the
user in plain language that you are about to pull third-party code that **Imbue
has not verified and that could be malicious** into their mind; name the repo
URL; and ask them to confirm they trust that source and want to proceed. Do NOT
fetch, merge, or run anything from the inspiration until they reply yes. If they
decline, stop here. This is informed consent, not a security guarantee -- you
are telling the user you cannot vouch for the code, not certifying it is safe.

The **template path (A)** needs no such gate: creating a mind from an
inspiration repo WAS the trust decision, so a mind already built from one is
treated as trusted -- go straight to adapting it.

## 1. Bring in the inspiration, verified in a worktree (merge path only)

Only after the trust gate (§0). The inspiration is unverified third-party code,
so NEVER merge it straight into the live tree: do the merge in an ISOLATED
worktree, confirm it went well there, and only then bring the verified result
into `/code`. This mirrors how `update-self` validates an upstream merge off the
live tree before landing it.

Do NOT use `git subtree add --prefix=.` — subtree does not support the repo root
as its prefix and errors out. First fetch the inspiration's branch (fetch only
moves objects into the local store; it changes no working tree):

```bash
git remote add inspiration <git-url>        # or a uniquely-named remote if 'inspiration' is taken
git fetch inspiration <branch>              # branch from the inspiration repo (default: main)
```

If the repo is private, the anonymous fetch fails with an auth error. Route git
through the latchkey gateway instead (it proxies GitHub's git endpoints with the
credential injected server-side; needs the `github-git` / `github-git-read`
permission -- initiate it yourself like any other latchkey permission request,
see the `latchkey` skill). Fetch the URL directly rather than persisting a
gateway-URL remote:

```bash
git -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    -c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
    fetch "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" <branch>
```

Now do the merge in a throwaway worktree branched off `HEAD`, and check it there
before it can touch the live tree:

```bash
WT="$(mktemp -d)"
git worktree add -q "$WT" HEAD
( cd "$WT" && git merge --allow-unrelated-histories --no-edit FETCH_HEAD )
```

**Check the merge went well, in the worktree:**

- **Merge conflicts** are HOLES, not a hard failure: they mark where the
  inspiration and this mind's tree disagree. Do NOT resolve them mechanically or
  land a half-merged tree -- remove the worktree (`git worktree remove --force
  "$WT"`), tell the user what conflicts (step 4, plain language), and only then
  redo the merge in `/code` and resolve it interactively with them.
- **Boot smoke-check** the merged worktree -- validate `supervisord.conf` WITHOUT
  launching the daemon (never `supervisord -t`, which launches it):

  ```bash
  ( cd "$WT" && python3 - <<'PYEOF'
  import sys
  try:
      from supervisor.options import ServerOptions
  except Exception:
      sys.exit(0)  # supervisor lib unavailable -- skip the check
  o = ServerOptions(); o.configfile = "supervisord.conf"
  o.realize(args=[]); o.process_config(do_usage=False)
  PYEOF
  )
  ```

  If this fails, the merged tree does not boot -- the inspiration broke this
  mind (a wiring mistake, or something hostile). STOP: tell the user plainly,
  remove the worktree, and do NOT bring it into `/code`.

**Land the verified result.** Only once the merge is clean and the boot check
passes, fast-forward `/code` onto the exact commit you checked, then remove the
worktree:

```bash
git merge --ff-only "$(git -C "$WT" rev-parse HEAD)"
git worktree remove --force "$WT"
```

This preserves both trees at the root. The inspiration's `inspiration-<slug>.md`
manifest(s) and their `.svg` thumbnails land at the repo root alongside anything
this mind already had.

This merge path does not touch `parent.toml` — provenance is read-only reference
(the inspiration records only a link to the default-workspace-template base it was
built from; there is no upstream fetch or pull here).

## 2. Read the relevant manifest

Locate the manifest at the repo root:

- Merge path: `inspiration-<slug>.md` for the inspiration you just merged in.
- Template path: `inspiration-<slug>.md` for the latest slug named in the
  repo's `/welcome` skill (or the one the user chose).

Read its front-matter (`title`, `description`, `thumbnail`, and optionally
`format`, the inspirations flow version that produced the manifest; manifests
published before versioning omit it, so treat an absent `format` as `v1`) and
its body sections:
`What it is`, `How it works`, `Prerequisites`, `How to adapt it`, `Holes`, and
`Adaptation history` (older manifests may have `Apps included` instead of `How
it works`, `Permissions it may need` instead of `Prerequisites`, and no `How to
adapt it`). Two distinct agendas: `Prerequisites` is the SETUP agenda —
machine-readable `requires_permission:` / `requires_secret:` lines you act on
to activate the app; `Holes` is the ADAPTATION agenda — design gaps the
original author left for the adapter.

## 3. Activate first, then ask how to adapt

In chat, in plain language, walk the user through what this inspiration provides
and what it needs from them — name the `Prerequisites` (do not enumerate file
paths at the user). Then ask whether they want to run it on the same connectors:
"This uses Slack to pull in messages — want me to connect it to your Slack now,
or would you rather it read something else, like email?"

**If they keep the same connectors — set it up BEFORE the adaptation
conversation:**

1. Initiate every `requires_permission:` line YOURSELF, now, via a latchkey
   permission request (see the `latchkey` skill: `latchkey curl -XPOST
   http://latchkey-self.invalid/permission-requests`; the request opens the
   approval/login flow in the minds app). Do not merely tell the user a
   permission is needed — send the request so it appears for them to approve.
2. Wire up any `requires_secret:` values (ask the user for them), start the
   services, and get the app running against THEIR data.
3. **Definition of done for a data-backed app: the user can open it and see
   their OWN data.** A service that starts cleanly or an endpoint that returns
   200 is NOT done — open the app's actual output yourself and confirm it
   shows the user's real content before saying it works.
4. Tell them it is live and invite them to take a look and play with it.

Only then ask: "Now — how would you like to adapt it?"

**If they want different connectors** (e.g. email instead of Slack), skip
activation and go straight to the adaptation conversation — the swap is the
first adaptation, and its new prerequisites get initiated the same way once
decided.

## 4. Fill holes interactively

Work through each hole with the user, one at a time. A hole is anything the
manifest flags as missing/stubbed, plus any merge conflict from step 1. Translate
each into non-technical terms, ask the user how they want it resolved when you are
unsure, and make the change. Only ask when you genuinely need a decision — resolve
the obvious ones yourself and keep moving.

## 5. Append a dated worksheet entry

The manifest is a worksheet. After adapting, **append** a dated entry to its
`Adaptation history` section — never rewrite the rest of the file. Append only:

```markdown
### <YYYY-MM-DD> — adapted by this mind
<what was changed / which holes were filled / decisions made>
```

Earlier history entries are left exactly as they are; each mind that adapts the
inspiration adds one more entry below the previous ones.

## 6. Accumulation

Merged-in `inspiration-*.md` manifests stay at the repo root alongside any that
were already here. Multiple inspirations coexist in one mind — bringing in a new
one never removes or overwrites the manifests of the others.

## 7. Commit

Commit the adaptation per the repo's git conventions (a plain local commit;
when the user has enabled GitHub sync, the post-commit hook handles any push).
Include the merged-in tree, the modified
files from filling holes, and the updated manifest with its new `Adaptation
history` entry.
