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

## 1. Bring in the inspiration (merge path only)

Bring the inspiration's tree into the current mind at the repo root.

Do NOT use `git subtree add --prefix=.` — subtree does not support the repo root
as its prefix and errors out. Instead, fetch the inspiration's branch and merge it
with unrelated histories, so both trees coexist at the root:

```bash
git remote add inspiration <git-url>        # or a uniquely-named remote if 'inspiration' is taken
git fetch inspiration <branch>              # branch from the inspiration repo (default: main)
git merge --allow-unrelated-histories --no-edit FETCH_HEAD
```

This preserves both trees at the root. The inspiration's `inspiration-<slug>.md`
manifest(s) and their `.svg` thumbnails land at the repo root alongside anything
this mind already had.

If the repo is private, the anonymous fetch above fails with an auth error.
Route git through the latchkey gateway instead (it proxies GitHub's git
endpoints with the credential injected server-side; needs the `github-git` /
`github-git-read` permission -- initiate it yourself like any other latchkey
permission request, see the `latchkey` skill). Fetch the URL directly rather
than persisting a gateway-URL remote:

```bash
git -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    -c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
    fetch "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" <branch>
git merge --allow-unrelated-histories --no-edit FETCH_HEAD
```

If the merge reports conflicts, do NOT try to resolve them mechanically. Each
conflict is a **hole**: a place where the inspiration and this mind's existing
tree disagree. Surface it to the user in plain, non-technical language (step 4)
and resolve it interactively.

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
