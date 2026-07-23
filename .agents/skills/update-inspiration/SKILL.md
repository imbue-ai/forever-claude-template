---
name: update-inspiration
description: Update an inspiration you already published -- re-cut the changes made since the last version on top of the published snapshot and advance the published repo by exactly one clean commit (v2, v3, ...), preserving all of the hand-crafted content in the published repo. Use when the user asks to update, re-publish, or ship a new version of an inspiration they previously published.
---

# Update a published inspiration

Version: v1 (inspirations flow). This is the PUBLISHER's update path -- the
companion to `publish-inspiration` (first publish, v1) and `use-inspiration`
(adopt someone else's). It produces the NEXT version (v2, v3, ...) of an
inspiration THIS mind already published: it re-cuts the changes the source
workspace has accumulated since the last version, lays them on top of the
published snapshot, and fast-forwards the published repo's `main` by exactly one
new commit -- while leaving every piece of hand-crafted content in the published
repo (the finished manifest prose, the bespoke thumbnail, the generated
`/welcome`, adopters' "Adaptation history") exactly as it was published.

Like `publish-inspiration`, this skill delegates the re-assembly to a
`launch-task` sub-agent worker (which builds the new snapshot in its own git
worktree), confirms with the user in chat at TWO gates, obtains GitHub access via
latchkey permissioning (never the `gh` CLI), and pushes -- directly from the
worker's worktree.

> **THE ONE SAFETY REQUIREMENT ABOVE ALL OTHERS -- DO NOT REGENERATE, RE-ASSEMBLE
> FROM THE PUBLISHED TIP.** An update is NOT a fresh publish. The published repo
> already holds content that only exists because a human and an agent made it: the
> finished "What it is" / "How it works" / "Recipe" / "Prerequisites" / "Holes"
> prose in `inspiration-<slug>.md`, the bespoke `inspiration-<slug>.svg`
> thumbnail, the inspiration-specific `/welcome`, and the adopters' "Adaptation
> history". A naive re-run of `build_inspiration.sh` RESETS to the raw `BASE_REF`
> and REGENERATES all of that from scratch (FILL-IN placeholders, the generic
> placeholder SVG, a fresh welcome, an empty adaptation history) -- it would
> DESTROY every one of those. **NEVER run `build_inspiration.sh` for an update,
> and never reset the assembly worktree to `BASE_REF`.** The update worker resets
> to the PUBLISHED TIP's tree (fetched from the repo) and overlays ONLY the
> user-confirmed changed paths on top of it, so everything hand-crafted survives
> untouched and only the app/feature changes advance. If you cannot get the
> published tip, STOP -- do not fall back to a from-`BASE_REF` rebuild.

> **CWD INVARIANT -- where each step runs.** The source mind's live checkout at
> `/code` is touched in exactly two read-only ways and one write:
> - §2 does a **read-only object fetch** of the published repo into `/code`'s
>   object store and reads the delta with `git diff`/`git show`. A fetch adds
>   objects; it NEVER changes `/code`'s working tree or branch. This is safe, and
>   is the same read-only fetch `use-inspiration` §1 uses.
> - Everything from §3's worker `done` through §7's push runs with **cwd = `$WT`**
>   (the worker's worktree), NEVER `/code`. There is no merge-back: `$WT`'s tree,
>   built by the worker on top of the fetched published tip, IS what gets pushed,
>   as-is, from `$WT`. IGNORE `lead-proxy.md`'s default `done -> merge the
>   worker's branch` handling -- as in `publish-inspiration`, merging the assembly
>   branch into `/code` is forbidden (it once reset a live mind's tree to an old
>   base). Do not reintroduce a merge, a `git checkout mngr/<slug>` in `/code`, or
>   any step that mutates `/code`'s tree after assembly.
> - **The ONE sanctioned write to `/code`: §8, the version-history entry.** After
>   the push SUCCEEDS, `/code` gets exactly one write -- appending the v(n+1)
>   entry to `VERSION_HISTORY.md` and committing that single file on the branch
>   `/code` is already on (`git add VERSION_HISTORY.md` + `git commit`, NEVER a
>   merge, checkout, reset, or `git add -A`). If the push did not happen, it does
>   not run at all.

> **AN INSPIRATION MUST STAY BOOTABLE -- NEVER PUBLISH A PARTIAL OR BROKEN
> UPDATE.** Every published version, including this one, must be the FULL bootable
> tree: the published snapshot with only the confirmed app/feature changes laid
> over it. If the fetch (§2), re-assembly (§3), the secret scan or boot check
> (§4), the chat confirmation or GitHub auth (§5-§7), or the push (§7) fails for
> ANY reason, do NOT invent an alternate mechanism -- no `gh api` file uploads, no
> hand-assembled subset, no force-push over the published history. Diagnose and
> fix the real blocker and retry the documented step, or STOP and tell the user
> plainly what failed and that you did not publish an update. A broken or partial
> "update" is strictly worse than leaving the existing published version alone.

## Shared conventions

- **The ledger** is `VERSION_HISTORY.md` at `/code`'s repo root, written by the
  `update-version` skill. Its `## Inspirations` section is where a publish/update
  is recorded; §1 reads it and §8 appends to it.
- **`$WT` -- the worker's worktree.** `mngr create` places worker worktrees under
  `/mngr/worktree/<name>-<uuid>/`; the path cannot be guessed -- resolve it from
  the worker's `done` report per §3. Everything after re-assembly runs with cwd =
  `$WT`.
- **The published tip.** The current `main` of the published repo -- the tree the
  update is built ON TOP of, and the parent of the one new commit. Fetched in §2
  and handed to the worker (§3).
- **The recipe** lives in the published `inspiration-<slug>.md`'s "## Recipe"
  block (`include` / `data_include` / `exclude` / `modification_rules`). It, not a
  repo-vs-repo diff, is the durable definition of how the inspiration is derived
  from its source -- an update re-runs it. §2 reads it out of the fetched tip.
- **Slug / repo-name rules** are identical to `publish-inspiration`'s (match
  `^[A-Za-z0-9._-]+$`, no leading `-`); an update never changes the slug or repo.

## 1. Locate the published inspiration

Read the ledger's `## Inspirations` section for the target slug's heading
`### <slug>  --  <repo-url>` and the version lines under it:

```bash
SLUG="<slug>"
SLUG="$SLUG" awk '
    $0 ~ "^### " ENVIRON["SLUG"] "  --  " { print; inside = 1; next }
    /^(## |### )/ { inside = 0 }
    inside' VERSION_HISTORY.md
```

From that block extract:

- **repo URL** -- from the `### <slug>  --  <repo-url>` heading (`github.com/<owner>/<repo>`);
- **current version `n`** -- the highest `v<k>` among the lines (the newest);
- **the source sha version `n` was cut from** -- the 7-char sha ending the newest
  `v<n>` line. This is the **anchor** for the "what changed since" diff.

**If there is no row for this slug** (the inspiration was published before this
feature existed, from another workspace, or the ledger was lost): do not guess.
Ask the user for the published repo's URL and confirm the slug, then reconstruct
a minimal anchor -- since there is no recorded source sha, §2's forward diff is
not computable, so instead fetch the published tip (§2) and treat its recipe as
the whole scope, asking the user which current paths they want refreshed. Note
plainly that without a recorded anchor you cannot show a precise "since v(n)"
delta, only the full current state of the recipe's paths.

## 2. Fetch the published tip, verify it, compute the delta, and run the scope gate

**cwd = `/code` for this section** (read-only fetch + `git diff`; nothing in
`/code`'s working tree changes).

**2a. Get read access and fetch the published tip.** Route git through the
latchkey gateway exactly as `use-inspiration` §1 does for a private fetch (the
`github-git` / `github-git-read` permission; initiate it yourself via a latchkey
permission request per the `latchkey` skill if the `permissions/self` probe shows
it missing, and tell the user an approval is waiting in minds). A public repo may
fetch anonymously.

```bash
git -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    -c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
    fetch "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" main
PUBLISHED_TIP="$(git rev-parse FETCH_HEAD)"
```

**2b. Verify the published repo is where we last left it (out-of-band-divergence
check).** The published tree should still be the clean v(n) snapshot this mind
pushed. Read the manifest at the tip and confirm its front-matter `version:`
equals `v<n>` (the ledger's current version), and that the tip is a single
inspiration snapshot on a template base (`git rev-list --count "$PUBLISHED_TIP"`
> 1, and its subject begins `inspiration:`):

```bash
git show "$PUBLISHED_TIP:inspiration-<slug>.md" | sed -n '1,20p'   # inspect: version: v<n>?
```

If the published manifest's version is HIGHER than the ledger's `n` (someone
published a newer version out of band), or the tip carries foreign commits an
adopter pushed (its subject is not an `inspiration:` snapshot, or unexpected
paths changed), **STOP and surface it to the user** -- do not overwrite an
out-of-band change. Reconcile first (re-read the ledger, or re-anchor on the
actual published version) before any update. (The shipped ledger records the
source sha, not the published snapshot sha, so this version-and-shape check is
the integrity gate in place of a recorded-snapshot-sha comparison.)

**2c. Read the recipe** out of the fetched tip's manifest -- the `include` /
`data_include` paths, the `exclude` list, and the `modification_rules` -- from the
"## Recipe" `yaml` block of `inspiration-<slug>.md`. These are the update's
inputs: the paths whose changes are eligible, and the rules to re-apply.

**2d. Compute the forward delta -- source side only.** Diff the recorded v(n)
source sha against the current workspace HEAD, scoped to the recipe's include
paths:

```bash
git diff --name-status <v(n)-source-sha>..HEAD -- <include path> [<include path> ...]
```

This is the **forward** delta -- only what changed in the source workspace since
v(n). NEVER diff the workspace against the published repo: the published tree has
had personal data stripped and modifications applied, so a workspace-vs-published
diff would try to re-add exactly the things the recipe deliberately removed. Also
note the **base delta**: compare the ledger's recorded base against the current
resolved base (`publish-inspiration` §2's marker walk). If `BASE_REF` moved, the
template substrate advanced too -- report it, but an app-delta update re-publishes
on the existing published base; re-cutting on a newer base is a separate, larger
operation (surface it as an option, default to not doing it).

**If the delta is empty and the base is unchanged**, the published version is
already current -- tell the user so and STOP. There is nothing to update.

**2e. The scope gate (hard gate -- confirm BEFORE any re-assembly).** Send ONE
message, in plain language:

- **what changed since v(n)** -- described as features/behavior, not a raw file
  list ("the sync bug fix and the new reminders column");
- **which of those changes will go into the update** -- the user may take the
  whole delta, a subset, or ALSO fold in a newly-created path that was not in the
  original recipe (adding a new path is a scope change to the recipe -- it
  re-opens the same include-set judgment `publish-inspiration` §1 makes, including
  the personal-data question for any data it carries);
- **what will NOT change** -- the recipe's existing exclusions still hold (they
  stay excluded even though they still exist in the workspace), the modifications
  re-apply, the thumbnail and prose stay as published unless the user asks, and
  visibility is unchanged;
- **the new version number** it will become (`v(n+1)`), and the base-delta note if
  any.

Then STOP your turn and WAIT for the user's explicit reply TO THIS message. The
user's "update my <slug>" request is NOT the go-ahead. Never declare scope
"confirmed" that the user has not answered.

## 3. Delegate the re-assembly to a launch-task worker

Do NOT dispatch until the user has replied to §2e. This is the "background agent"
that implements the update: a `launch-task` sub-agent on its own worktree
(`mngr/<slug>`). Per `launch-task`, the whole delegation is ONE step in your
timeline.

**There is no `build_inspiration.sh --update` mode** -- the assembly script only
knows how to reset to `BASE_REF` and regenerate everything, which is exactly what
an update must NOT do. So the worker performs the re-assembly with explicit git
steps (mirroring `build_inspiration.sh`'s mechanics -- stage-before-reset,
`read-tree`/`clean`, `rsync` overlay, hard secret scan, boot smoke-check, single
`commit-tree`) but with the reset target and the overlay set changed.

**3a. Hand the worker the published tip while keeping it OFFLINE.** The worker
must assemble from the published tip's tree, but -- like `build_inspiration.sh` --
it does no network fetch itself. Bundle the tip you already fetched in §2 and
stage it into the worker via `launch-task`'s `source_artifacts_dir` mechanism (a
gitignored artifact pushed into the worker's worktree):

```bash
mkdir -p runtime/launch-task/<slug>
git tag -f "_pub-tip-<slug>" "$PUBLISHED_TIP"
git bundle create runtime/launch-task/<slug>/published-tip.bundle "_pub-tip-<slug>"
git tag -d "_pub-tip-<slug>"
```

Doing the fetch + integrity check in the lead (§2, in chat) and handing the tip
over as a frozen bundle is deliberate: it keeps the worker offline (matching
`build_inspiration.sh`'s no-fetch invariant), keeps all user-facing GitHub auth
in the lead (matching `publish-inspiration`), and lets divergence (§2b) be
surfaced in chat rather than discovered deep inside the worker.

**3b. Commit pending `/code` work, then write the task file and launch.** (`mngr
create` refuses a dirty tree; commit -- never stash.) Substitute the real values:
the slug, repo URL, `PUBLISHED_TIP` sha, the user-confirmed changed paths as a
`--name-status` delta (added / modified / deleted, scoped to what the user
approved in §2e, plus any newly-added path), the `exclude` list and
`modification_rules` from §2c, the new version `v(n+1)`, and a one-line
description of what changed for the changelog entry.

Set `source_artifacts_dir: runtime/launch-task/<slug>` in the task frontmatter so
the bundle is pushed to the worker. The task body directs the worker to:

1. **Parse the frontmatter FIRST** (`LEAD_AGENT` / `FINISH_REPORT_PATH`) per
   `.agents/shared/references/worker-reporting.md` -- before any reset that could
   remove the task file.
2. **Load the published tip from the bundle** and confirm it matches the expected
   sha (objects only; no network):
   ```bash
   git fetch runtime/launch-task/<slug>/published-tip.bundle "refs/tags/_pub-tip-<slug>:refs/tags/_pub-tip-<slug>"
   test "$(git rev-parse refs/tags/_pub-tip-<slug>)" = "<PUBLISHED_TIP>"   # abort if not
   ```
3. **Snapshot the secret-scan tools and stage the approved changed paths BEFORE
   resetting** (the reset removes the current-source versions from the worktree,
   exactly as `build_inspiration.sh` step 1 stages includes first). Copy
   `.agents/skills/publish-inspiration/scripts/scan_secrets.sh` and its sibling
   `betterleaks.toml` out to a scratch dir, and `rsync -aR` every ADDED/MODIFIED
   approved path (from the current-HEAD checkout) into a stage dir. Record the
   DELETED approved paths as a list.
4. **Reset the worktree to the PUBLISHED TIP -- never `BASE_REF`:**
   ```bash
   git read-tree -u --reset "<PUBLISHED_TIP>"
   git clean -fdxq
   ```
   This is the line that preserves every hand-crafted thing: the manifest prose,
   the "## Recipe", the thumbnail SVG, the `/welcome`, and the adopters'
   "Adaptation history" are all present in the published tip and are now the
   working tree.
5. **Apply the approved delta on top of the published tip, and nothing else.**
   Overlay the staged added/modified paths (`rsync -a "$STAGE/" "$REPO/"`,
   root-to-root -- the trailing slash matters, never a nesting copy), and `rm` the
   recorded deleted paths from the tree. Do NOT touch any path outside the
   approved set. Then re-apply the recipe: skip anything under an `exclude` entry,
   and re-apply each `modification_rule` to the freshly-overlaid content (the
   overlay re-introduces the source's real value -- e.g. a hardcoded channel -- so
   the rule must re-generalize it, exactly as at first publish; the rules are
   stored as rules, not values, precisely so they can be replayed here).
6. **Re-run the hard secret scan over every overlaid/modified path** with the
   snapshotted tool -- a secret introduced in the source since v(n) can ride in on
   an updated path just as at first publish:
   ```bash
   bash "$SCAN_TOOLS_DIR/scan_secrets.sh" "$STAGE"
   ```
   Any finding, scanner error, or missing scanner -> fix or report `stuck`; never
   commit around it. This stays the authoritative, hard-failing blocker.
7. **Update the manifest -- append only, never regenerate.** In
   `inspiration-<slug>.md`: bump the front-matter `version:` to `v(n+1)` and the
   "## Recipe" `version:` to `v(n+1)` (and add any newly-included path / new
   `modification_rule` the user approved). Append ONE entry to the END of the
   "## Publication history" section:
   `### v(n+1) (YYYY-MM-DD) -- <one line: what changed since v(n)>` (today's
   date). Newest last; NEVER rewrite an earlier Publication-history entry, and
   NEVER write into "Adaptation history" (that is the adopters' log). Leave the
   thumbnail as published unless the lead's task says the user wants it changed.
8. **Boot smoke-check** the result -- validate `supervisord.conf` via the
   supervisor lib (`ServerOptions().realize()` / `process_config()`), NEVER
   `supervisord -t` (which launches the daemon), the same method
   `build_inspiration.sh` step 9 uses. If it fails, report `stuck`.
9. **Mint ONE clean commit parented on the published tip:**
   ```bash
   git add -A
   SNAPSHOT_COMMIT="$(git commit-tree "$(git write-tree)" -p "<PUBLISHED_TIP>" -m "inspiration: <slug> v(n+1)

   Update of the <slug> inspiration on top of the published v(n) snapshot; app-delta re-cut, recipe re-applied.")"
   git merge-base --is-ancestor "<PUBLISHED_TIP>" "$SNAPSHOT_COMMIT"   # must pass
   test "$(git rev-list --count "$SNAPSHOT_COMMIT")" -gt 1              # must pass
   git reset --soft "$SNAPSHOT_COMMIT"
   ```
   Parenting on the published tip -- which itself descends from `BASE_REF` -- means
   the published `main` advances by EXACTLY ONE commit, `merge-base(template,
   tip)` stays `BASE_REF` (composability preserved), and the mint is a single
   atomic post-cleanup snapshot: no pre-scan / pre-generalization state ever
   exists as its own commit.
10. **Self-check and report `done`** with the worktree's absolute path and the
    branch `mngr/<slug>` -- the lead pushes from there. There must be NO
    `<!-- FILL-IN` markers and NO `minds-placeholder-thumbnail` marker
    reintroduced (a correct update never regenerates those), and `git status` must
    be clean.

Launch the worker and background-await its report exactly as `publish-inspiration`
§3 / `launch-task` §2-§4 do. Handle the report per `lead-proxy.md` with the same
override: on `done`, do NOT merge `mngr/<slug>`; resolve `$WT`, verify the
worker's gates yourself, then proceed to §5 with cwd = `$WT`. On `stuck`, surface
the reason and stop (or fix the input and relaunch); publish nothing.

## 4. What the worker guarantees (guard rails)

The worker reports `done` only when: the tree is the published tip plus exactly
the approved delta (nothing hand-crafted regenerated), the secret scan passed over
every overlaid/modified path, the boot smoke-check passed, the manifest's
Publication history gained exactly one new `v(n+1)` entry (earlier entries and
Adaptation history untouched), and the single snapshot commit is parented on the
published tip. A `stuck` report maps to the same causes as
`publish-inspiration` §5 (secret scan, boot check) plus: bundle sha mismatch, or
the published tip could not be loaded -- all "fix the input and relaunch", never
"publish something smaller".

## 5. Confirm the update in chat (final gate)

**cwd = `$WT`.** This is the second hard gate. An update publishes NEW content to
the user's account, so no earlier approval carries over -- not the §2e scope gate,
not the GitHub permission approval. Present the proposal ONCE, in plain language:

- a short recap of **what changed** in this version (the confirmed delta), and the
  **new version number** `v(n+1)`;
- the **modifications re-applied** (so the user can verify their earlier removals
  still hold in the new version);
- the **thumbnail** -- state it is unchanged from the published version, and embed
  it only if the user asked to change it (`![<title> thumbnail]($WT/inspiration-<slug>.svg)`);
- the **visibility is unchanged** -- an update never changes it. Restate it so the
  user sees it is not silently flipping.

Then END YOUR TURN and WAIT for the user's explicit go-ahead TO THIS message.
Your own gate checks are verification, not confirmation. If the user asks for
edits (e.g. a thumbnail tweak or a Publication-history wording change), apply them
in `$WT` -- keeping the SVG safety rules from `publish-inspiration` §6 -- and
commit in `$WT` before §7. If the user aborts, stop and leave the assembled
commit intact.

## 6. Ensure GitHub push access (latchkey -- not the gh CLI)

Reuse `publish-inspiration` §7's mechanics for the WRITE side. You already have
`github-git-read` from §2a; the push needs `github-git-write`. Probe it and, if
missing, initiate the permission request yourself (`payload.scope: github-git`,
`permissions: ["github-git-write"]`), tell the user an approval is waiting in
minds, and background-poll (bounded) until granted. No `github-rest-api` /
repo-creation grant is needed -- the repo already exists and this flow does not
create one or change its settings. Never fall back to a token-in-URL push.

## 7. Fast-forward push v(n+1), then move the tag

**cwd = `$WT`.** The push publishes the update.

- **Pre-push checklist:** `(cd "$WT" && git status)` must be clean; the
  placeholder-thumbnail / FILL-IN / SVG-safety greps from `publish-inspiration` §8
  must print NOTHING (a correct update never reintroduces a placeholder).
- **Push as a fast-forward -- no force.** The minted commit descends from the
  current published tip, so `main` advances by one commit and no history is
  rewritten:

  ```bash
  ( cd "$WT" \
      && git merge-base --is-ancestor "<PUBLISHED_TIP>" "$SNAPSHOT_COMMIT" \
      && test "$(git rev-list --count "$SNAPSHOT_COMMIT")" -gt 1 \
      && git \
      -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
      -c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
      push "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" "${SNAPSHOT_COMMIT}:refs/heads/main" )
  ```

  A NON-fast-forward rejection means the published `main` moved since §2b's check
  (a genuine out-of-band push) -- STOP and surface it; do NOT `--force`. Handle
  the other push-failure causes (permission, HTTP 413, `workflow` scope, GitHub
  push-protection on the baked-in Minds Google OAuth client) exactly as
  `publish-inspiration` §8's "Failure handling" list does.

- **Move / create the version tag** (the design's `inspiration/<slug>/v<n>` tag
  scheme -- the durable, adopter-facing version marker). Publish v1 did not push a
  v1 tag, so in practice this CREATES the tag at v(n+1); it is create-or-move and
  idempotent:

  ```bash
  ( cd "$WT" \
      && git tag -f -a "inspiration/<slug>/v(n+1)" "$SNAPSHOT_COMMIT" -m "inspiration <slug> v(n+1)" \
      && git \
      -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
      -c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
      push "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo>.git" "refs/tags/inspiration/<slug>/v(n+1)" )
  ```

  If the tag push fails, the update itself already succeeded -- retry once, and
  otherwise report it as a minor follow-up rather than failing the update.

## 8. Record the v(n+1) entry in the ledger (ONLY after the push succeeded)

The single sanctioned write back to `/code` -- read the CWD-INVARIANT callout at
the top before running it. If the push failed or the user aborted, SKIP this
entirely: an update that did not publish is never recorded.

Append the entry per the **`update-version`** skill's §3 (append an
`## Inspirations` entry) with `SLUG=<slug>`,
`REPO_URL="github.com/<owner>/<repo>"`, `NOTE="<one line: what changed>"`, and
`SOURCE_SHA` = the current `/code` HEAD the update was cut from (the source anchor
for v(n+1) -- NOT `BASE_REF`, NOT `PUBLISHED_TIP`, NOT anything from `$WT`).
`update-version` computes the version number from the existing lines under the
slug's heading, so it appends `v(n+1)` automatically. Then commit that one file:

```bash
( cd /code \
    && git add VERSION_HISTORY.md \
    && git commit -m "version history: updated inspiration <slug> to v(n+1)" )
```

Exactly one file staged by name, one commit, on whatever branch `/code` is on.
NEVER `git add -A`, never a merge/checkout/reset. It is a no-op if the entry is
already recorded (a retried step cannot double-record). If the commit fails (a
hook rejects it), the update still succeeded -- say so and fix the entry rather
than re-pushing anything.

## 9. Close out

On a successful push, clean up per `launch-task`: the worker can be destroyed now
(`create_worker.py destroy --name <slug>`), and the local `mngr/<slug>` branch can
go (the snapshot commit lives on the remote; the branch's intermediate commits
were never pushed). Remove the bundle artifact
(`runtime/launch-task/<slug>/published-tip.bundle`). No git remote cleanup is
needed -- §2a/§7 fetch and push explicit URLs and add no named remote.

If the push failed and you are stopping, leave the worker, `$WT`, and the branch
intact for a retry. Report the updated repo URL and new version to the user in
your final message.

## Invariants preserved (call them out)

- **Bootable-or-nothing.** Every published version, including this update, is the
  full bootable tree; a failed step publishes nothing.
- **Preserve the user's customizations.** The update re-assembles from the
  PUBLISHED TIP and overlays only the approved delta -- the finished manifest
  prose, "## Recipe", thumbnail, `/welcome`, and adopters' "Adaptation history"
  are never regenerated. `build_inspiration.sh` is never run for an update.
- **One atomic post-cleanup commit.** The mint is a single `commit-tree` from the
  final, scanned, generalized tree, parented on the published tip -- no pre-scan
  or pre-generalization state ever exists as its own commit, and `merge-base(template, tip)`
  stays `BASE_REF`.
- **Private-by-default; visibility never changes on an update**, and the final
  gate restates it.
- **The hard secret scan is the authoritative blocker** -- re-run over every
  overlaid/modified path, hard-failing.
- **Both chat gates run** -- the §2e scope gate and the §5 final gate; no earlier
  approval substitutes for either.
