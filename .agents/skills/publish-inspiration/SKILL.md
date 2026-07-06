---
name: publish-inspiration
description: Publish a clean, shareable snapshot of the apps/features this mind built to a new GitHub repo (an "inspiration" another mind can adapt). Use when the user asks to publish, share, or export what they built as a reusable template.
---

# Publish an inspiration

An "inspiration" is a clean, shareable, **bootable** snapshot of the apps and
features this mind built, published to a new GitHub repo so another mind can
be created FROM it (not just read its app code). One repo can accumulate
several inspirations (one manifest + thumbnail per inspiration, all at the
repo root). This skill assembles the snapshot on a clean template base,
confirms the details with the user in chat, and (on confirmation) creates the
repo and pushes.

The assembly + smoke-check run in an isolated local `git worktree` in this same
container (no sub-agent -- it is a fast, deterministic script, not work that
warrants delegation). You own the chat confirmation, the GitHub auth, and the
push. There is NO in-UI popup anywhere in this flow: everything the user
confirms or changes happens in the chat conversation.

> **CWD INVARIANT -- read this before running anything in §§6-8.** From the
> moment §3's `git worktree add` succeeds, the live mind's checkout at `/code`
> is DONE being touched for the rest of this skill. Every command in §6
> (chat confirmation), §7 (GitHub auth), and §8 (create repo + push) --
> including any follow-up commit that writes the confirmed
> thumbnail/title/description
> -- runs with **cwd = `$WT`** (the assembly worktree), NEVER `/code`. There is
> no merge-back step: `$WT`'s tree, built by `build_inspiration.sh` on top of
> `BASE_REF`, IS the tree that gets pushed, as-is. `/code`'s branch and working
> tree are never modified, merged into, or pushed from. This is the single most
> important invariant in this skill: a prior version of this skill merged the
> assembly branch into `/code`'s current branch before pushing, which one time
> silently reset `/code`'s entire live tree to an old base (a normal 3-way
> merge diffs from the merge-base, and the assembled tree looks nothing like
> `/code`'s HEAD, so git read everything present in HEAD but absent from the
> old base as an intentional deletion). Do not reintroduce a merge, a
> `git checkout mngr/<slug>` in `/code`, or any other step that runs from
> `/code` after assembly.

> **AN INSPIRATION MUST BE BOOTABLE -- NEVER PUBLISH A PARTIAL SNAPSHOT.** A
> valid inspiration is always the FULL tree `build_inspiration.sh` assembles on
> `mngr/<slug>`: the clean FCT base (`pyproject.toml`, `supervisord.conf`,
> `.mngr/`, `.agents/skills/` including the rewritten `/welcome`, `parent.toml`,
> etc.) plus the selected app/feature paths -- never just the app code plus a
> README. That full tree is what makes `/use-inspiration`'s template path work:
> another mind must be creatable FROM the published repo, not merely able to
> read its source. If assembly (§3), the chat confirmation or GitHub auth (§6-§7), or the
> push (§8) fails for ANY reason, do NOT invent an alternate publish mechanism
> -- do not push a hand-assembled subset of files via `gh api`, a plain `git
> init` of just the app directory, or any other ad-hoc path outside this
> skill's documented flow. A non-bootable "inspiration" silently defeats the
> whole feature and is strictly worse than no publish at all. Instead: diagnose
> and fix the actual blocker (e.g. re-resolve `BASE_REF` per §2, retry
> assembly, pick a different repo name) and retry the documented flow from the
> failed step, or STOP and clearly tell the user what failed, why, and that you
> did not publish -- never silently redefine what "publishing an inspiration"
> means.

## Shared conventions

- **Slug derivation.** `slug` = the user's title lowercased, with each run of
  characters outside `[A-Za-z0-9._-]` collapsed to a single `-`, and leading /
  trailing `-` stripped. The result MUST match `^[A-Za-z0-9._-]+$` and MUST NOT
  start with `-` (the backend re-validates). `repo_name` defaults to `slug`;
  the user may override it during the chat confirmation (§6). The same
  slug names the manifest (`inspiration-<slug>.md`), the thumbnail
  (`inspiration-<slug>.svg`), and the assembly branch (`mngr/<slug>`).
- **`BASE_REF` (provenance + clean base).** The FCT commit this mind was
  created from. Resolve it **in-repo, with no network access** (see step 2); do
  NOT `git fetch`/`git pull` upstream. Pass it to `build_inspiration.sh` as
  `--base-ref`.

## 1. Setup Q&A (live in chat)

Ask the user, in plain language, three things. Never enumerate files at them:

- a name for the inspiration (this becomes the title, and the slug is derived
  from it);
- which apps or features they want to include (you translate this into a set of
  repo-root-relative include paths, e.g. `apps/slack-inbox`, `libs/slack_inbox`,
  plus their service wiring -- you reason about the backing paths, the user does
  not);
- whether any data should be included. **Default: NO user data.** Include data
  paths only if the user explicitly asks for them.

Derive `slug` and `repo_name` from the title. Resolve the concrete set of
include paths yourself.

## 2. Resolve `BASE_REF` (in-repo, no network)

`BASE_REF` is the FCT base commit this mind was created from -- the most recent
commit whose subject starts with `update-self:` (the same `update-self:` subject
convention `update-self` / `assist` rely on), or, if there is none, the
**first-parent root**:

```bash
git rev-list --first-parent HEAD | tail -1
```

The fallback MUST be the first-parent root, never a bare root-commit lookup
(`git rev-list --max-parents=0 HEAD`): subtree merges add parallel root commits
that are NOT the seed (a mind repo can have several near-empty roots), while
the first-parent chain from HEAD always ends at the true template seed. Do NOT
fetch or pull from upstream to obtain it -- `parent.toml` is a provenance link
only.

**Mandatory pre-check (before ANY assembly).** Verify the resolved base is a
bootable template -- its tree must name both `pyproject.toml` and
`supervisord.conf` -- AND that it already carries the `/welcome` inspiration
takeover markers (`<!-- INSPIRATION:BEGIN -->` / `<!-- INSPIRATION:END -->` as
exact whole lines in `.agents/skills/welcome/SKILL.md`), since the assembly
script's `/welcome` rewrite (§4's item 9, "the assembly script" section below)
needs them to drive round-2 adaptation on boot -- a base that predates the
markers silently degrades that feature even though it still boots:

```bash
git ls-tree --name-only "<BASE_REF>^{tree}" | grep -qx pyproject.toml \
  && git ls-tree --name-only "<BASE_REF>^{tree}" | grep -qx supervisord.conf \
  && git show "<BASE_REF>:.agents/skills/welcome/SKILL.md" 2>/dev/null | grep -qxF -- '<!-- INSPIRATION:BEGIN -->' \
  && git show "<BASE_REF>:.agents/skills/welcome/SKILL.md" 2>/dev/null | grep -qxF -- '<!-- INSPIRATION:END -->'
```

If the check fails, STOP and reconsider the base (e.g. walk forward along the
first-parent chain to the earliest commit that passes all four checks, or ask
the user) rather than launching assembly -- this catches the wrong-root and
too-old-base problems in seconds instead of a full assembly round-trip.
`build_inspiration.sh` re-validates all four conditions itself and exits 5 with
a clear message (see §5), but that is a backstop, not a substitute for the
pre-check.

## 3. Assemble on a local git worktree (same container, no sub-agent)

Assembly is a fast, deterministic script (`build_inspiration.sh`, ~a second), so
run it yourself on a throwaway `git worktree` in THIS container. Do NOT delegate
it to a `launch-task` sub-agent: that spins up a second agent with its own
boot/read/report/poll loop -- minutes of latency for a sub-second job -- and adds
no isolation, because a `git worktree` already gives a clean, separate working
tree while leaving the live mind (`/code`) completely untouched. Open one step:

```bash
tk create --step "Assemble the shareable inspiration snapshot"
# -> Created cod-step-XXXX: ...
tk start cod-step-XXXX
```

Use a **deterministic** worktree path (so later steps can find it) and a fresh
`mngr/<slug>` branch, then run the assembly script inside it. The script `cd`s to
its own worktree root, so its `git read-tree -u --reset` + `git clean -fdxq`
rewrite ONLY that worktree, never `/code`:

```bash
WT="/tmp/inspiration-<slug>"
git worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"   # clear any stale worktree
git branch -D "mngr/<slug>" 2>/dev/null || true
git worktree add -q -b "mngr/<slug>" "$WT" HEAD
( cd "$WT" && bash /code/.agents/skills/publish-inspiration/scripts/build_inspiration.sh \
    --base-ref <BASE_REF> \
    --slug <slug> \
    --title "<title>" \
    --description "<description>" \
    --include <path> [--include <path> ...] \
    [--data-include <path> ...] )
```

Check the subshell's exit code and handle the guard rails directly (§5): a
non-zero exit means nothing was committed -- surface the reason to the user and
stop. On success the assembled commit is on `mngr/<slug>`, checked out at
`$WT`, and `$WT` is HEAD -- there is no merge-back into `/code`.

**Flesh out the manifest -- mandatory, before §6.** `$WT/inspiration-<slug>.md`
has `<!-- FILL-IN (publishing agent): ... -->` comment blocks in "What it is,"
"How it works," "Holes," and "Permissions it may need" -- these are generated
placeholders, not real content, and the script's closing summary reminds you of
this every time. Open the file and replace EVERY block with real, specific
content: for "Holes" and "Permissions it may need" in particular, think through
what the included apps actually depend on -- an external API token, an OAuth
app, a Slack/Discord/etc. workspace installation, a hardcoded account or
channel -- and name it explicitly. If a section genuinely has nothing to add
(no holes, no permissions needed), say so explicitly in prose; never leave the
placeholder comment in place and never leave a section blank. This is the next
agent's entire agenda for the adaptation conversation (see the manifest's own
"How to adapt it" section) -- an inspiration that needs a Slack token but
doesn't say so silently breaks adoption. Commit this edit in `$WT` (cwd = `$WT`,
same as everything else after assembly).

**Mandatory check -- do not skip.** Before the chat confirmation (§6), confirm no
placeholders remain:

```bash
grep -l -- '<!-- FILL-IN (publishing agent)' "$WT/inspiration-<slug>.md" \
  && echo "STOP: finish every FILL-IN section in the manifest before publishing"
```

If this reports a match, go back and finish the manifest -- do not proceed to
§6 with unfinished sections.

**Craft the thumbnail -- mandatory, before §6.** The
`inspiration-<slug>.svg` the assembly script generates is a generic
placeholder and MUST NOT be what the user is shown in §6 or what gets
published. Replace it with an SVG you draw yourself that looks like the
actual app: reproduce its real layout (header, panels, cards, chart shapes --
whatever the app actually shows), its real color palette, and its real title
text, so someone browsing inspirations recognizes the app at a glance. Base
it on what the app genuinely renders -- open the running app (or its
templates/CSS) and mirror what you see. Keep it small and self-contained
(inline shapes and text only; mock data, never real user data, and no
embedded rasters or external references -- `<script>`/`<foreignObject>` get
stripped anyway). Write it over `$WT/inspiration-<slug>.svg` and commit in
`$WT` along with the manifest edit.

Once both the manifest and thumbnail are done, close the assembly step
and proceed straight to §6 (the chat confirmation), §7 (GitHub auth), and §8
(create repo + push), ALL running with **cwd = `$WT`** (see the callout
above).

## 4. What the assembly does

`build_inspiration.sh` (documented below) does the whole assembly in the
worktree: clean base + overlay + secret scan + manifest + thumbnail + `/welcome`
rewrite + boot smoke-check + a single commit. It communicates purely via its
exit code -- `0` on success (the assembled commit is on `mngr/<slug>`), non-zero
otherwise (see §5). It prints a summary of what it assembled to stderr.

## 5. Guard rails (the script's non-zero exits)

- **Secret scan (exit 1).** A credential/token rode in on an overlaid path.
  Nothing was committed; surface the flagged path (value redacted) and stop.
- **No-diff guard (exit 3).** The resolved include set contributes nothing
  beyond `BASE_REF` (the assembled tree equals the base tree). Tell the user
  plainly and do NOT create a repo -- there are no empty inspiration repos.
- **Boot smoke-check (exit 4).** The clean base does not boot at all; abort
  BEFORE any repo creation. Selected apps having holes is expected and does NOT
  fail the check.
- **Non-template base (exit 5).** The `--base-ref` does not resolve to a tree
  in the repo, or its tree is not a bootable template: it lacks
  `pyproject.toml` and/or `supervisord.conf` (e.g. a parallel subtree root was
  picked instead of the real seed), or it lacks the `/welcome` inspiration
  takeover markers (`<!-- INSPIRATION:BEGIN -->` / `<!-- INSPIRATION:END -->`
  as exact whole lines in `.agents/skills/welcome/SKILL.md`) needed to drive
  round-2 adaptation on boot. Nothing was committed; re-resolve `BASE_REF` per
  §2 (its pre-check should have caught this before assembly).

Every one of these is a "fix the input and retry the script" situation, never
a "publish something smaller instead" situation -- see the "MUST BE BOOTABLE"
callout at the top of this skill.

## 6. Confirm the details in chat

**cwd = `$WT` for this and every remaining section.** The manifest/thumbnail
files referenced below (`inspiration-<slug>.md` / `.svg`) live at `$WT`'s repo
root, not `/code`'s.

There is no popup. Confirm everything in the chat conversation, in plain
language:

1. Present the proposal: the title, the description, the repo name, the
   visibility (default private), and the thumbnail. For the thumbnail, give
   the user an actual look at it, not a description of markup -- e.g. share
   the rendered `$WT/inspiration-<slug>.svg` via the `file-sharing` skill or
   any other way the user can SEE the image -- and briefly say what it shows.
2. Ask the user to confirm or change any of these. Keep it to one compact
   message; never enumerate files or paste SVG markup at them.
3. Apply requested changes and re-confirm:
   - Title / description / repo name / visibility: apply directly (re-derive
     nothing; the slug stays fixed -- it is the immutable identity of this
     proposal, so a title change does NOT rename the manifest/thumbnail/branch).
   - Thumbnail: the user describes what they want in plain language ("darker",
     "show the calendar view", "use the app's green"); YOU redraw the SVG
     (same rules as the "Craft the thumbnail" step in §3: the app's real look,
     mock data only) and show it again. NEVER ask the user to edit or supply
     SVG markup -- this is a non-technical app, and you are the only one who
     touches markup.
4. Loop until the user clearly confirms ("yes, publish it") or aborts. Do not
   proceed to §7/§8 on a lukewarm or ambiguous reply, and do not publish
   without an explicit confirmation. If the user aborts, stop and leave the
   assembled commit intact.

Keep the SVG you commit free of active content (`<script>`, `on*` handler
attributes, `<foreignObject>`): you author it yourself, so this is simply a
rule about what you write, not a sanitization step.

**Commit before §8's push.** Once the user has confirmed, write the final SVG
and any edited title/description into `$WT/inspiration-<slug>.svg` /
`$WT/inspiration-<slug>.md` and COMMIT that change with cwd = `$WT` before
proceeding to §7/§8. Never push first and fix up the thumbnail or manifest
with a second commit-and-re-push. This commit -- like everything else in this
skill after assembly -- happens IN `$WT`, never `/code`.

## 7. Ensure GitHub auth (no popup, no agent restart)

GitHub auth uses the regular credential paths -- the latchkey-provisioned
token or a terminal login -- there is NO login popup to raise. Work through
these in order, and verify each step's outcome before moving to the next so
the flow never silently stalls:

1. **Check the current state first:**

   ```bash
   gh auth status --hostname github.com
   ```

   If it reports logged in AND the token scopes shown include `workflow`,
   you're done -- skip to §8. The `workflow` scope is mandatory: the template
   ships `.github/workflows/`, and GitHub rejects pushing those files without
   it, so a token that authenticates but lacks `workflow` still fails §8 --
   treat it as not-logged-in and continue down this list.

2. **Latchkey / environment token.** The container normally receives a
   GitHub token as `GH_TOKEN` via the minds credential plumbing (latchkey).
   If `GH_TOKEN` is set but step 1 failed, the token is stale or
   under-scoped. Ask the user (in chat) to grant GitHub access through the
   app: request it per the `latchkey` skill (`latchkey services info github`,
   then a permission request via `latchkey curl -XPOST
   http://latchkey-self.invalid/permission-requests`), which triggers the
   login flow on the user's side. Then re-run step 1 to verify. If latchkey
   does not support GitHub in this deployment, say so and move to step 3 --
   do not stall here.

3. **Terminal device-flow login (always works):**

   ```bash
   env -u GH_TOKEN -u GITHUB_TOKEN gh auth login --hostname github.com \
       --git-protocol https --web --skip-ssh-key --scopes workflow
   ```

   (The `env -u` scrub matters here: with a stale `GH_TOKEN` still exported,
   `gh` would keep preferring it over the fresh login.) Run it as a
   background/pty task (it blocks waiting for the browser step), surface the
   printed one-time code and `https://github.com/login/device` to the user in
   chat, then poll `env -u GH_TOKEN -u GITHUB_TOKEN gh auth status --hostname
   github.com` until it reports authenticated -- for a SINGLE bounded window
   of ~90 seconds, run as a BACKGROUND task (mirror `launch-task`'s
   background-await pattern, §3 of `.agents/skills/launch-task/SKILL.md`; a
   foreground `while` loop sleeping ~90s can be killed by your own
   tool-execution timeout, exit 137/144, silently abandoning the wait). Then
   run `env -u GH_TOKEN -u GITHUB_TOKEN gh auth setup-git` to wire the git
   credential helper. If the user never completes the login, surface a clear
   message and stop, leaving the assembled commit intact.

Whichever path succeeded, re-verify with step 1's command before §8 -- do not
assume auth worked because a command exited 0. If the login came from step 3,
the `env -u GH_TOKEN -u GITHUB_TOKEN` scrub must also prefix the §8
`gh`/`git` commands so the stored credential (not a stale env token) is used.
Do NOT restart the agent or re-source the environment.

## 8. Create the repo and push

**cwd = `$WT`.** This is the step that actually publishes -- it MUST run from
the assembly worktree so `gh repo create --source=.` picks up `$WT`'s tree
(the clean assembled snapshot), never `/code`'s.

With `repo_name` / `visibility` taken from the confirmed response:

- **Pre-push checklist:** `(cd "$WT" && git status)` must be clean -- the
  confirmed thumbnail/manifest edits from §6 are already committed in `$WT`,
  nothing uncommitted remains. If anything is dirty, commit it first (in
  `$WT`); never push and then fix up with a re-push.

```bash
( cd "$WT" && env -u GH_TOKEN -u GITHUB_TOKEN gh repo create "<repo_name>" --<visibility> --source=. --remote=inspiration --push )
```

(`--private` or `--public` per `visibility`. `repo_name` is validated
`^[A-Za-z0-9._-]+$` server-side, which blocks argument injection, but still pass
it as a single argv element -- never interpolate it into a shell string. The
`env -u GH_TOKEN -u GITHUB_TOKEN` prefix is load-bearing: it forces `gh`/`git` to
use the credential a §7 terminal login stored via `setup-git`, not a stale
`GH_TOKEN` inherited by your agent shell. `--source=.` resolves relative to the
`cd "$WT"` in the same subshell, so it always means `$WT`, never `/code`.)

**Tag the repo (immediately after a successful create+push).** Every published
inspiration carries the `minds-inspiration` GitHub topic -- a repo topic, NOT
part of the description -- so inspirations are discoverable as a group (e.g.
`gh search repos --topic minds-inspiration`, or GitHub's topic page). Also set
the repo's description from the confirmed `description`:

```bash
( cd "$WT" && env -u GH_TOKEN -u GITHUB_TOKEN gh repo edit "<owner>/<repo_name>" --add-topic minds-inspiration --description "<description>" )
```

(GitHub topic rules: lowercase letters, digits, and hyphens only -- the fixed
literal `minds-inspiration` already conforms; do not prefix it with `#`. Take
`<owner>` from the `gh repo create` output or `gh repo view --json owner`. If
this edit fails, the publish itself already succeeded -- retry the edit once,
and if it still fails, report it as a minor follow-up rather than treating the
publish as failed.)

**Failure handling.** If `gh repo create` fails (e.g. the name is taken, or the
token lacks the `workflow` scope needed to push `.github/workflows/` -- see
§7), report it to the user and re-confirm in chat (§6)
for a new name / visibility, keeping the assembled commit intact in `$WT`. Loop
until it succeeds or the user aborts. **Never fall back to publishing a
different, non-bootable thing** (e.g. pushing just the selected app files via
`gh api` instead of `$WT`'s full assembled tree) -- see the "MUST BE BOOTABLE"
callout at the top of this skill. If you cannot get the documented flow to
succeed, stop and report the blocker; do not improvise a substitute publish.

## 9. Accumulation

Publishing a mind that already holds `inspiration-*.md` manifests plus their app
dirs carries ALL of them forward into the new repo alongside the newly-published
one -- they are part of the assembled tree. The `/welcome` rewrite targets only
the newly-published slug (the latest).

## 10. Close out

On a successful push, remove the throwaway worktree and its branch -- the
commit is fully preserved on the new remote, so keeping a stray local branch of
an old snapshot around in `/code` is just clutter:

```bash
git worktree remove --force "$WT"
git branch -D "mngr/<slug>"
```

If the push failed and you are stopping (user aborted, unrecoverable error),
leave `$WT` and the `mngr/<slug>` branch intact instead -- do not delete work
the user may want to retry or reassemble from.

Close the assembly step with a work-summary line. Report the new repo URL in
your final assistant message to the user (not in the step summary).

## The assembly script: `scripts/build_inspiration.sh`

You run `scripts/build_inspiration.sh` inside the assembly worktree (§3). It is
self-contained (the dev `create-new-mind-repo` recipe is NOT available in the
VM). Interface (cwd = worktree repo root):

```
.agents/skills/publish-inspiration/scripts/build_inspiration.sh \
  --base-ref <BASE_REF> \          # FCT commit the mind was based on (provenance + clean base)
  --slug <slug> \
  --title <title> \
  --include <path> [--include <path> ...] \   # repo-root-relative app/feature paths to overlay
  [--data-include <path> ...] \    # only when the user opted in; default none
  [--description <text>]
```

What it does, in order (see the script for the exact commands):

1. Validates that the `--base-ref` tree names `pyproject.toml` and
   `supervisord.conf` (a bootable template base) AND carries the `/welcome`
   inspiration takeover markers in `.agents/skills/welcome/SKILL.md` (needed
   for round-2 adaptation on boot); exits 5 with a clear message otherwise,
   before touching the worktree (see §5).
2. Stages the selected paths out of the current live-mind worktree into a
   scratch dir (preserving relative paths) BEFORE resetting.
3. Resets the worktree to the clean base with
   `git read-tree -u --reset <BASE_REF>` then `git clean -fdxq` -- this drops
   tracked-but-not-in-base files AND gitignored cruft (secrets, runtime state).
   It never `git checkout <ref> -- .` (that leaks the mind's whole committed
   tree) and never fetches/pulls upstream.
4. Overlays the staged paths onto the clean base with
   `rsync -a "$STAGE/" "$REPO/"` (root-to-root contents merge) -- never a
   nesting copy like `cp -a "$STAGE/apps" "$REPO/apps"`.
5. Carries forward any existing accumulated `inspiration-*.md` + `.svg` at the
   repo root.
6. Runs a deterministic secret scan that HARD-FAILS (non-zero, abort before any
   commit/push) on token patterns and credential filenames. This is the
   authoritative blocker, not LLM prose.
7. Generates the manifest `inspiration-<slug>.md` at the repo root.
8. Generates a placeholder thumbnail `inspiration-<slug>.svg` (mock data only;
   you MUST overwrite it with a hand-drawn SVG that looks like the actual
   app before the chat confirmation -- see §3's "Craft the thumbnail").
9. Rewrites only the marked stable region of `welcome/SKILL.md` to describe the
   newly-published inspiration.
10. Validates `supervisord.conf` WITHOUT starting the daemon (never
    `supervisord -t`), then makes a single commit for the assembled snapshot.
