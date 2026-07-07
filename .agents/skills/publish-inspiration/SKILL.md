---
name: publish-inspiration
description: Publish a clean, shareable snapshot of the apps/features this mind built to a new GitHub repo (an "inspiration" another mind can adapt). Use when the user asks to publish, share, or export what they built as a reusable template.
---

# Publish an inspiration

An "inspiration" is a clean, shareable, **bootable** snapshot of the apps and
features this mind built, published to a new GitHub repo so another mind can
be created FROM it (not just read its app code). One repo can accumulate
several inspirations (one manifest + thumbnail per inspiration, all at the
repo root). This skill delegates the assembly to a `launch-task` sub-agent
worker (which builds the snapshot, finishes the manifest, and designs the
thumbnail in its own git worktree), confirms the publish with the user in
chat, obtains GitHub access via latchkey permissioning (never the `gh` CLI),
and then creates the repo and pushes -- directly from the worker's worktree.

> **CWD INVARIANT -- read this before running anything in §§6-8.** From the
> moment §3's worker reports `done`, the live mind's checkout at `/code` is
> DONE being touched for the rest of this skill. Every command in §6 (chat
> confirmation and any confirmed manifest/thumbnail edits), §7 (GitHub auth),
> and §8 (create repo + push) runs with **cwd = `$WT`** (the worker's
> worktree), NEVER `/code`. There is no merge-back step: `$WT`'s tree, built
> by `build_inspiration.sh` on top of `BASE_REF` and finished by the worker,
> IS the tree that gets pushed, as-is, by you, from `$WT`. In particular,
> IGNORE `lead-proxy.md`'s default `done -> merge the worker's branch`
> handling for this flow -- `/code`'s branch and working tree are never
> modified, merged into, or pushed from. This is the single most important
> invariant in this skill: a prior version of this skill merged the assembly
> branch into `/code`'s current branch before pushing, which one time
> silently reset `/code`'s entire live tree to an old base (a normal 3-way
> merge diffs from the merge-base, and the assembled tree looks nothing like
> `/code`'s HEAD, so git read everything present in HEAD but absent from the
> old base as an intentional deletion -- 1400+ files gone from a live mind).
> Do not reintroduce a merge, a `git checkout mngr/<slug>` in `/code`, or any
> other step that runs from `/code` after assembly.

> **AN INSPIRATION MUST BE BOOTABLE -- NEVER PUBLISH A PARTIAL SNAPSHOT.** A
> valid inspiration is always the FULL tree `build_inspiration.sh` assembles on
> `mngr/<slug>`: the clean FCT base (`pyproject.toml`, `supervisord.conf`,
> `.mngr/`, `.agents/skills/` including the generated inspiration `/welcome`, `parent.toml`,
> etc.) plus the selected app/feature paths -- never just the app code plus a
> README. That full tree is what makes `/use-inspiration`'s template path work:
> another mind must be creatable FROM the published repo, not merely able to
> read its source. If assembly (§3), the chat confirmation or GitHub auth
> (§6-§7), or the push (§8) fails for ANY reason, do NOT invent an alternate
> publish mechanism -- do not push a hand-assembled subset of files via `gh
> api`, a plain `git init` of just the app directory, or any other ad-hoc path
> outside this skill's documented flow. A non-bootable "inspiration" silently
> defeats the whole feature and is strictly worse than no publish at all.
> Instead: diagnose and fix the actual blocker (e.g. re-resolve `BASE_REF` per
> §2, relaunch the worker, pick a different repo name) and retry the
> documented flow from the failed step, or STOP and clearly tell the user what
> failed, why, and that you did not publish -- never silently redefine what
> "publishing an inspiration" means.

## Shared conventions

- **Slug derivation.** `slug` = the user's title lowercased, with each run of
  characters outside `[A-Za-z0-9._-]` collapsed to a single `-`, and leading /
  trailing `-` stripped. The result MUST match `^[A-Za-z0-9._-]+$` and MUST NOT
  start with `-` (`build_inspiration.sh` re-validates). `repo_name` defaults to
  `slug`; the user may override it in the chat confirmation (§6) -- validate
  any override against the same pattern yourself. The same slug names the
  manifest (`inspiration-<slug>.md`), the thumbnail (`inspiration-<slug>.svg`),
  the assembly worker, and the worker's branch (`mngr/<slug>`).
- **`$WT` -- the worker's worktree.** `mngr create` places worker worktrees
  under `/mngr/worktree/<name>-<uuid>/` (the `worktree_base_folder` in
  `.mngr/settings.toml`; the `<uuid>` suffix is random), so the path cannot be
  guessed -- resolve it after the worker's `done` report per §3. Everything
  after assembly runs with cwd = `$WT` (see the callout above).
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
`supervisord.conf`:

```bash
git ls-tree --name-only "<BASE_REF>^{tree}" | grep -qx pyproject.toml \
  && git ls-tree --name-only "<BASE_REF>^{tree}" | grep -qx supervisord.conf
```

If the check fails, STOP and reconsider the base (e.g. walk forward along the
first-parent chain to the earliest commit that passes both checks, or ask
the user) rather than launching the worker -- this catches the wrong-root and
too-old-base problems in seconds instead of a full worker round-trip.
`build_inspiration.sh` re-validates both conditions itself and exits 5 with
a clear message (see §5), but that is a backstop, not a substitute for the
pre-check.

## 3. Delegate assembly to a launch-task worker

Assembly runs in a `launch-task` sub-agent worker. The worker gets a fresh git
worktree on branch `mngr/<slug>`, runs `build_inspiration.sh` there, then --
in the SAME run, no second round-trip -- fleshes out every manifest FILL-IN
block and designs the bespoke thumbnail. `/code` is never modified.

The worker name is `<slug>`. Names must be unique: if a previous attempt left
a worker or branch with this name, clean it up first
(`uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>`,
then `git branch -D "mngr/<slug>"` once no worktree holds it).

Per `launch-task`, the whole delegation is ONE step in your timeline:

```bash
tk create --step "Delegate assembling the shareable inspiration snapshot to a sub-agent"
# -> Created cod-step-XXXX: ...
tk start cod-step-XXXX
```

**Write the task file.** Substitute the real `<slug>`, `<title>`,
`<description>`, `<BASE_REF>`, and include paths into the body -- the worker
must be able to run the script verbatim, with zero back-and-forth:

````bash
mkdir -p runtime/launch-task/<slug>
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/launch-task/<slug>/reports/report.md
---
FRONTMATTER_EOF
cat << 'BODY_EOF'

# Task: Assemble the "<title>" inspiration snapshot

## What to do

Assemble a clean, bootable "inspiration" snapshot on your worktree's branch,
then finish its manifest and thumbnail. Do ALL of it in this one run.

**Before anything else**, extract `LEAD_AGENT` / `FINISH_REPORT_PATH` per
`.agents/shared/references/worker-reporting.md`: step 1's script resets your
worktree to a clean template base and deletes gitignored state -- including
`runtime/` and this task file -- so parse the frontmatter FIRST.

1. **Run the assembly script** from your worktree root, verbatim (every value
   below was already resolved by the lead):

   ```bash
   bash .agents/skills/publish-inspiration/scripts/build_inspiration.sh \
       --base-ref <BASE_REF> \
       --slug <slug> \
       --title "<title>" \
       --description "<description>" \
       --include <path> [--include <path> ...] \
       [--data-include <path> ...]
   ```

   On ANY non-zero exit: nothing was committed; go straight to "Reporting
   back" with a `stuck` report that quotes the script's stderr verbatim (the
   exit code maps to a specific guard rail the lead knows how to handle). Do
   NOT retry with different arguments and do NOT assemble anything by hand.

2. **Flesh out the manifest.** `inspiration-<slug>.md` at the repo root has
   `<!-- FILL-IN (publishing agent): ... -->` comment blocks in "What it is,"
   "How it works," "Prerequisites," and "Holes" -- generated placeholders,
   not real content. Replace EVERY block with real, specific content.
   "Prerequisites" is the strictest: one machine-readable line per activation
   requirement in the exact `requires_permission:` / `requires_secret:` forms
   the template shows, derived from the included code (inspect every service
   the app reaches through `latchkey curl` and name the real latchkey scope
   and permission schema, e.g. `slack-api / slack-read-all`). These lines are
   what the ADOPTING agent acts on during setup -- it initiates each one via
   a latchkey permission request before asking how to adapt -- so a vague or
   missing line silently breaks adoption (a real incident: an adopter never
   prompted for a Slack permission the app needed). "Holes" is the
   adaptation agenda only -- design gaps, stubbed integrations, hardcoded
   accounts -- never activation requirements. If a section genuinely has
   nothing to add, say so explicitly in prose; never leave a placeholder
   comment in place and never leave a section blank.

3. **Design the thumbnail.** `inspiration-<slug>.svg` at the repo root is a
   generic placeholder the script generated -- it must never be published.
   Replace its entire contents with a bespoke SVG you design for THIS app: a
   clean, simple, iconic representation of what the app actually is and shows
   (derive it from the app code and the manifest you just wrote -- e.g. a
   stylized miniature of its main screen or its core object). Hard rules:
   mock data only, never real user data; no `<script>`; no `on*=` event
   attributes; no `<foreignObject>`; no external references (no href/src
   pointing outside the file) -- fully self-contained. Keep the root
   `viewBox` around 240x160.

4. **Commit** the manifest + thumbnail edits as a follow-up commit on your
   branch (`mngr/<slug>`), in your worktree.

5. **Self-check, then report.** Both greps must print NOTHING before you may
   report `done`:

   ```bash
   grep -n -- '<!-- FILL-IN (publishing agent)' inspiration-<slug>.md
   grep -nEi -- 'minds-placeholder-thumbnail|<script|<foreignObject|on[a-z]+[[:space:]]*=' inspiration-<slug>.svg
   ```

   If either prints anything, fix and re-commit; do not report done until
   both are clean and `git status` is clean.

## Context

- Your worktree is a fresh checkout on branch `mngr/<slug>`. The script
  resets it to the clean template base `<BASE_REF>` and overlays only the
  selected paths, so the final tree looks nothing like the live mind's HEAD
  -- that is correct and expected. Do not "restore" anything it removes.
- Included paths and what each one is:
  <one line per include path: what it is and its role>
- <extra context the lead has: what the app does for its user, known holes,
  tokens/accounts it depends on -- everything the worker needs to write a
  good manifest and a representative thumbnail>

## Success criteria

- `build_inspiration.sh` exited 0 and its commit is on `mngr/<slug>`.
- Every FILL-IN block replaced with real prose (or an explicit "none").
- `inspiration-<slug>.svg` is a bespoke design for this app; the placeholder
  marker is gone and the safety grep is clean.
- Follow-up edits committed on `mngr/<slug>`; `git status` clean.

## Reporting back

Follow `.agents/shared/references/worker-reporting.md` for the full report
procedure. Substitutions for this task:

- `<TASK_FILE_GLOB>` -> `runtime/launch-task/*/task.md`
- `<RUNTIME_REPORTS_DIR>` -> `runtime/launch-task/<slug>/reports/` (recreate
  it with `mkdir -p` -- the assembly script deleted `runtime/`)
- Valid `name:` values: `question` (mid-flight gate), `done` / `stuck`
  (terminal).

In a `done` report body, include your worktree's absolute path (from
`git rev-parse --show-toplevel`) and the branch `mngr/<slug>` -- the lead
publishes directly from that worktree. In a `stuck` report, quote the
assembly script's stderr verbatim.
BODY_EOF
} > runtime/launch-task/<slug>/task.md
````

**Launch** (foreground, so a failed launch surfaces immediately):

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name <slug> \
    --template worker \
    --runtime-dir runtime/launch-task/<slug>/ \
    --task-file runtime/launch-task/<slug>/task.md
```

**Background-await the report** (Bash `run_in_background: true` -- never block
on it), then continue with whatever else you were doing:

```bash
# Run with Bash run_in_background: true
uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --task-file runtime/launch-task/<slug>/task.md
```

**Handle the report** per `.agents/shared/references/lead-proxy.md` (proxy or
answer any `question` gate, consume reports into `consumed/`, diagnose
liveness on a timeout) -- with one critical override:

- `name: stuck` -> the assembly script refused for one of §5's reasons.
  Surface the quoted stderr to the user plainly and stop (or fix the input --
  e.g. re-resolve `BASE_REF` per §2 -- and relaunch). Do not publish anything.
- `name: done` -> do **NOT** merge `mngr/<slug>` (that is `lead-proxy.md`'s
  default `done` handling, and it is exactly the merge the CWD-INVARIANT
  callout forbids -- the assembled tree diffs against `/code` as mass
  deletions). Instead, resolve `$WT`:

  ```bash
  WT="$(git worktree list --porcelain | awk -v b='refs/heads/mngr/<slug>' '$1 == "worktree" { wt = $2 } $1 == "branch" && $2 == b { print wt }')"
  ```

  Cross-check it against the worktree path in the report body (worktrees live
  under `/mngr/worktree/<slug>-<uuid>/`), then verify the worker's gates
  yourself -- both greps must print nothing and `git -C "$WT" status` must be
  clean:

  ```bash
  grep -n -- '<!-- FILL-IN (publishing agent)' "$WT/inspiration-<slug>.md"
  grep -nEi -- 'minds-placeholder-thumbnail|<script|<foreignObject|on[a-z]+[[:space:]]*=' "$WT/inspiration-<slug>.svg"
  ```

  If either grep hits, message the worker to finish the job (per
  `lead-proxy.md`'s gate mechanics) rather than finishing it yourself.
  Once clean, close the delegation step and proceed to §6 (chat
  confirmation), §7 (GitHub auth), and §8 (create repo + push), ALL running
  with **cwd = `$WT`** (see the callout above).

Leave the worker itself alone until §10 -- destroying it removes `$WT`, which
you still need for the push.

## 4. What the assembly does

`build_inspiration.sh` (documented below) does the whole mechanical assembly
in the worker's worktree: clean base + overlay + secret scan + manifest +
placeholder thumbnail + an inspiration-specific `/welcome` written into the
snapshot + boot smoke-check + a single
commit. It communicates purely via its exit code -- `0` on success (the
assembled commit is on `mngr/<slug>`), non-zero otherwise (see §5). It prints
a summary of what it assembled to stderr. The worker then supplies the two
things the script cannot: the manifest prose and the bespoke thumbnail.

## 5. Guard rails (the script's non-zero exits)

The worker maps any non-zero exit to a `stuck` report quoting the script's
stderr. What each exit means, and what you do:

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
  picked instead of the real seed). Nothing was committed; re-resolve
  `BASE_REF` per
  §2 (its pre-check should have caught this before launch) and relaunch.

Every one of these is a "fix the input and relaunch the worker" situation,
never a "publish something smaller instead" situation -- see the "MUST BE
BOOTABLE" callout at the top of this skill.

## 6. Confirm the publish in chat

**cwd = `$WT` for this and every remaining section.** The manifest/thumbnail
files referenced below (`inspiration-<slug>.md` / `.svg`) live at `$WT`'s repo
root, not `/code`'s.

Confirmation happens inline in chat -- there is no other confirmation
mechanism. Present the proposal to the user ONCE, in plain language:

- the **title** and **description**;
- the **repo name** (defaults to `slug`);
- the **visibility** (default: **private**);
- the **thumbnail** the sub-agent designed -- EMBED it in the chat message
  as a markdown image so the user actually sees what will represent their
  inspiration, using the file's absolute path:

  ```markdown
  ![<title> thumbnail]($WT/inspiration-<slug>.svg)
  ```

  (substitute the real absolute worktree path), and note you can adjust it if
  they'd like.

Let the user edit any of these in their replies, then proceed with the agreed
values. Do not re-ask what they already answered in §1; this is a
confirm-and-adjust pass, not a second interview. If the user asks to abort,
stop here and leave the assembled commit intact (§10's failure path).

- Validate an edited repo name against `^[A-Za-z0-9._-]+$` (no leading `-`)
  before using it.
- If the user asks for thumbnail changes, YOU edit
  `$WT/inspiration-<slug>.svg`, keeping the same safety rules the worker
  followed: mock data only, no `<script>`, no `on*=` attributes, no
  `<foreignObject>`, no external references. If the user pastes raw SVG
  markup in chat, never write it into the file verbatim -- apply the same
  rules first (strip anything that violates them, and tell the user what you
  stripped).

**Commit before §8's push.** Write any confirmed title/description edits into
`inspiration-<slug>.md`'s front-matter (and any thumbnail edits into the
`.svg`), and COMMIT that change with cwd = `$WT` before proceeding to §7/§8.
Never push first and fix up the manifest or thumbnail with a second
commit-and-re-push. This commit -- like everything else in this skill after
assembly -- happens IN `$WT`, never `/code`.

## 7. Ensure GitHub access (latchkey -- do NOT use the gh CLI)

GitHub access goes through **latchkey's github permissioning**, exactly like
every other connector in this template (see the `latchkey` skill). Do NOT use
the `gh` CLI anywhere in this flow -- no `gh auth`, no `gh repo` -- and do not
run browser/device login flows. Latchkey keeps the credential outside the
container and injects it per-request; the user approves once in the minds app.

Probe whether GitHub API access is already permitted:

```bash
latchkey curl https://api.github.com/user
```

If that fails with missing credentials or "request not permitted by the
user", initiate a permission request for the github scope YOURSELF (the
request opens the approval/login flow in the minds app):

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
    -H 'Content-Type: application/json' \
    -d '{"scope": "github-rest-api", "permissions": ["github-read-repos", "github-write-repos"], "rationale": "Publish this inspiration as a new GitHub repo on your account."}'
```

Tell the user in chat that a GitHub approval is waiting for them in minds,
then poll the probe **as a background task, bounded** (mirror `launch-task`'s
background-await pattern; a foreground `while` loop can be killed by your own
tool-execution timeout):

```bash
# Run with Bash run_in_background: true -- bounded (~5 minutes), one wait, no re-arm thrash
for _ in $(seq 1 30); do
    if latchkey curl https://api.github.com/user >/dev/null 2>&1; then
        echo "github access: permitted"
        exit 0
    fi
    sleep 10
done
echo "github access: still not permitted" >&2
exit 1
```

If the user never approves, surface a clear message and stop, leaving the
assembled commit intact.

**The push credential is separate.** Latchkey covers every GitHub API call
(repo creation, topics, description -- see §8), but a `git push` is not an
HTTP API call latchkey can inject into, and latchkey deliberately never hands
the raw token to the container. The push authenticates with the mind's
standard `GH_TOKEN` (the same credential the post-commit auto-push hook
uses). Check it now:

```bash
[ -n "$GH_TOKEN" ] && echo "GH_TOKEN present" || echo "GH_TOKEN MISSING"
```

If `GH_TOKEN` is missing, tell the user plainly: the repo and its metadata
can be created via latchkey, but pushing the git history needs `GH_TOKEN`
configured for this mind -- and stop rather than improvising (see the "MUST
BE BOOTABLE" callout; never upload a partial tree through the API instead).
Note the token must carry the `workflow` scope: the template ships
`.github/workflows/`, and GitHub rejects pushes of those files without it.

## 8. Create the repo and push

**cwd = `$WT`.** This is the step that actually publishes -- it MUST run from
the worker's worktree so the push sends `$WT`'s assembled branch (the clean
snapshot), never anything from `/code`.

With `repo_name` / `visibility` taken from the chat confirmation:

- **Pre-push checklist:**
  - `(cd "$WT" && git status)` must be clean -- the confirmed
    thumbnail/manifest edits from §6 are already committed in `$WT`, nothing
    uncommitted remains. If anything is dirty, commit it first (in `$WT`);
    never push and then fix up with a re-push.
  - **Placeholder-thumbnail gate** -- this grep must print NOTHING:

    ```bash
    grep -nEi -- 'minds-placeholder-thumbnail|<script|<foreignObject|on[a-z]+[[:space:]]*=' "$WT/inspiration-<slug>.svg"
    ```

    A `minds-placeholder-thumbnail` hit means the script's placeholder is
    still in place (the bespoke thumbnail never landed); the other patterns
    are the SVG safety rules. On ANY hit, block the push, fix the file (a
    real bespoke SVG, rules applied), commit in `$WT`, and re-run the gate.

Publish in TWO steps -- create the repo via the GitHub API through latchkey,
then push the assembled branch with git. (Historical note: this flow once used
`gh repo create --source=.`, which both violates the no-gh rule and breaks
inside git worktrees, whose `.git` is a file.)

**Step 1 -- create the repo (latchkey, sets name + description + visibility
in one call):**

```bash
latchkey curl -X POST https://api.github.com/user/repos \
    -H 'Content-Type: application/json' \
    -d '{"name": "<repo_name>", "description": "<description>", "private": <true|false>}'
```

Take `<owner>` from the response's `.owner.login`. `"private"` is `true` for
the default private visibility, `false` only if the user chose public. You
already validated `repo_name` against `^[A-Za-z0-9._-]+$` in §6; keep the
JSON built from variables, never string-interpolated shell.

**Step 2 -- push the assembled branch as `main` (git + `GH_TOKEN`):**

```bash
( cd "$WT" && git push "https://x-access-token:${GH_TOKEN}@github.com/<owner>/<repo_name>.git" "mngr/<slug>:main" )
```

The refspec `mngr/<slug>:main` pushes the assembled branch as the new repo's
`main` regardless of anything else, so the published tree is exactly `$WT`'s
snapshot. The token rides in the URL for this one command -- do not echo the
URL, do not write it into git config or a named remote (nothing to clean up
afterward, and the token never lands on disk).

**Step 3 -- tag the repo (immediately after a successful push).** Every
published inspiration carries the `minds-inspiration` GitHub topic -- a repo
topic, NOT part of the description -- so inspirations are discoverable as a
group (topic search / GitHub's topic page):

```bash
latchkey curl -X PUT "https://api.github.com/repos/<owner>/<repo_name>/topics" \
    -H 'Content-Type: application/json' \
    -d '{"names": ["minds-inspiration"]}'
```

(GitHub topic rules: lowercase letters, digits, and hyphens only -- the fixed
literal `minds-inspiration` already conforms; do not prefix it with `#`. If
this call fails, the publish itself already succeeded -- retry once, and if
it still fails, report it as a minor follow-up rather than treating the
publish as failed.)

**Failure handling.** If the create fails (e.g. the name is taken), ask in
chat for a new name and retry step 1. If the push fails (e.g. `GH_TOKEN`
missing the `workflow` scope needed for `.github/workflows/`), fix the cause
and retry step 2 -- do NOT re-create the repo. Keep the assembled commit
intact in `$WT` throughout; loop until it succeeds or the user aborts.
**Never fall back to publishing a different, non-bootable thing** (e.g.
uploading just the selected app files through the API instead of pushing
`$WT`'s full assembled tree) -- see the "MUST BE BOOTABLE" callout at the top
of this skill. If you cannot get the documented flow to succeed, stop and
report the blocker; do not improvise a substitute publish.

## 9. Accumulation

Publishing a mind that already holds `inspiration-*.md` manifests plus their app
dirs carries ALL of them forward into the new repo alongside the newly-published
one -- they are part of the assembled tree. The generated `/welcome` targets
only the newly-published slug (the latest).

## 10. Close out

On a successful push, clean up per `launch-task`'s conventions: the worker can
be destroyed now (destroying it removes its worktree, i.e. `$WT`), and the
local branch can go too -- the commit is fully preserved on the new remote:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git worktree prune
git branch -D "mngr/<slug>"
```

(No git remote cleanup is needed: §8 pushes to an explicit URL and never adds
a named remote.)

If the push failed and you are stopping (user aborted, unrecoverable error),
leave the worker, `$WT`, and the `mngr/<slug>` branch intact instead -- do not
delete work the user may want to retry or reassemble from.

Close the delegation step with a work-summary line. Report the new repo URL in
your final assistant message to the user (not in the step summary).

## The assembly script: `scripts/build_inspiration.sh`

The worker runs `scripts/build_inspiration.sh` from its worktree root (§3). It
is self-contained (the dev `create-new-mind-repo` recipe is NOT available in
the VM). Interface (cwd = worktree repo root):

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
   `supervisord.conf` (a bootable template base); exits 5 with a clear
   message otherwise, before touching the worktree (see §5).
2. Stages the selected paths out of the worker's checkout into a scratch dir
   (preserving relative paths) BEFORE resetting.
3. Resets the worktree to the clean base with
   `git read-tree -u --reset <BASE_REF>` then `git clean -fdxq` -- this drops
   tracked-but-not-in-base files AND gitignored cruft (secrets, runtime state,
   including the worker's `runtime/` task file). It never
   `git checkout <ref> -- .` (that leaks the mind's whole committed tree) and
   never fetches/pulls upstream.
4. Overlays the staged paths onto the clean base with
   `rsync -a "$STAGE/" "$REPO/"` (root-to-root contents merge) -- never a
   nesting copy like `cp -a "$STAGE/apps" "$REPO/apps"`.
5. Carries forward any existing accumulated `inspiration-*.md` + `.svg` at the
   repo root.
6. Runs a deterministic secret scan that HARD-FAILS (non-zero, abort before any
   commit/push) on token patterns and credential filenames. This is the
   authoritative blocker, not LLM prose.
7. Generates the manifest `inspiration-<slug>.md` at the repo root (with the
   FILL-IN blocks the worker must replace).
8. Generates a placeholder thumbnail `inspiration-<slug>.svg` carrying a
   distinctive `minds-placeholder-thumbnail` marker comment; the worker MUST
   replace the whole file with a bespoke SVG before reporting done, and the
   marker makes §8's pre-push gate a deterministic grep.
9. Overwrites the snapshot's `welcome/SKILL.md` with a generated
   inspiration-specific welcome describing the
   newly-published inspiration.
10. Validates `supervisord.conf` WITHOUT starting the daemon (never
    `supervisord -t`), then makes a single commit for the assembled snapshot.
