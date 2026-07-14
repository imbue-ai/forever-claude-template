---
name: publish-inspiration
description: Publish a clean, shareable snapshot of the apps/features this mind built to a new GitHub repo (an "inspiration" another mind can adapt). Use when the user asks to publish, share, or export what they built as a reusable template.
---

# Publish an inspiration

Version: v1 (inspirations flow). This versions the publish/adopt flow and the
`inspiration-<slug>.md` manifest format.

An "inspiration" is a clean, shareable, **bootable** snapshot of the apps and
features this mind built, published to a new GitHub repo so another mind can
be created FROM it (not just read its app code). One repo can accumulate
several inspirations (one manifest + thumbnail per inspiration, all at the
repo root). This skill delegates the assembly to a `launch-task` sub-agent
worker (which builds the snapshot, finishes the manifest, and designs the
thumbnail in its own git worktree), opens a local preview of the assembled
snapshot as an `inspiration-preview` tab, confirms the publish with the user
in chat, obtains GitHub access via latchkey permissioning (never the `gh`
CLI), and then creates the repo and pushes -- directly from the worker's
worktree. The preview tab is torn down at close-out (§10), success or abort.

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
>
> The ONE documented exception is §6's preview-tab plumbing (and its §10
> teardown): `render_preview.sh` renders into a temp dir, and
> `scripts/forward_port.py` runs from `/code` because it writes the live
> mind's gitignored `runtime/applications.toml` tab registry. Those commands
> never run git and never touch tracked files in `/code` -- the invariant
> above is about `/code`'s tree and branches, which stay untouched.

> **AN INSPIRATION MUST BE BOOTABLE -- NEVER PUBLISH A PARTIAL SNAPSHOT.** A
> valid inspiration is always the FULL tree `build_inspiration.sh` assembles on
> `mngr/<slug>`: the clean DEFAULT_WORKSPACE_TEMPLATE base (`pyproject.toml`, `supervisord.conf`,
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
- **`BASE_REF` (provenance + clean base).** The workspace's creation snapshot
  -- the template state this mind started from (or last updated itself to).
  Resolve it **in-repo, with no network access** (see step 2); do NOT
  `git fetch`/`git pull` upstream. Pass it to `build_inspiration.sh` as
  `--base-ref`.

## 1. Setup Q&A and the scope gate (live in chat)

Ask the user, in plain language. Never enumerate files at them:

- which apps or features they want to include (you translate this into a set of
  repo-root-relative include paths, e.g. `apps/slack-inbox`, `libs/slack_inbox`,
  plus their service wiring -- you reason about the backing paths, the user does
  not);
- what should and should not be SHARED -- **ask, never silently assume**.
  The default is still NO user data, but apply it as a question, not a
  policy: name the kinds of shareable content you actually found in the
  selected paths, in plain language rather than file lists ("your saved
  contacts", "the instructions you wrote for the digest", sample data,
  docs), and ask which of these, if any, they want to ship. Some users
  WANT their instructions or curated examples shared -- do not strip those
  without asking; equally, do not ship data without an explicit yes;
- whether anything should be **changed, removed, or generalized in the
  published version only** -- hardcoded personal preferences, account or
  channel names, anything they'd rather not ship. Their live files stay
  untouched; the edits land only in the snapshot (see the modifications step
  in §3);
- a name for the inspiration (propose one yourself; it becomes the title, and
  the slug derives from it -- naming is cheap to change later, so it never
  needs to hold up the gate below).

Derive `slug` and `repo_name` from the title. Resolve the concrete set of
include paths yourself.

**Design the adopter's onboarding, then confirm it.** Before the gate below,
think through the first-run experience of someone ADOPTING this inspiration,
and write it down as a short plain-language onboarding summary. Design for
the smoothest path to an INSTANT app the adopter can see working with their
own data and then modify:

- Every piece of data the app needs must come from the adopter's own
  connectors when one exists, initiated by the adopting agent via latchkey --
  NEVER a manual data-entry chore. (A real publish told adopters to type all
  their contacts into a `contacts.txt`; the right onboarding pulls contacts
  from their email/Google account after a permission approval. Manual entry
  is a design failure whenever a connector could supply the same data.)
- Enumerate what the adopting agent will self-initiate (the
  `requires_permission:` / `requires_secret:` lines), what then populates
  automatically, and what genuinely remains for the adopter to decide --
  those decisions are the Holes; data-entry chores are not acceptable Holes.
- The summary reads like: "when someone adopts this, their agent asks to
  connect X and Y; the app then fills with their own data automatically; the
  only choices left are Z."

This summary is confirmed at the gate below and then handed to the worker
(§3), which writes the manifest's Prerequisites / How to adapt it / Holes to
MATCH it. (The generated welcome routes adopters through the manifest, so
the manifest matching is sufficient -- the welcome itself is a deterministic
write the worker never edits.)

**The scope gate: confirm BEFORE any assembly work -- before treating the
include set as final, and before dispatching the worker (§3). This is a hard
gate.** Send ONE message that lays out, in plain language:

- what WILL be included (apps/features, not file lists);
- what will NOT be included that they might expect (their data, other apps
  this mind has, secrets/config) -- so surprises surface now;
- the share-or-strip answers from the Q&A above, restated (which
  instructions/config/sample data ship, which data stays private) -- as
  their answers being played back, not as assumptions;
- the **onboarding summary** you designed above -- how an adopter goes from
  creating a mind off this repo to seeing the app working with their own
  data, what their agent self-initiates, and what the remaining Holes are;
- the published-version modifications you will apply (or "none");
- the proposed title and repo name, marked as adjustable later;
- the default private visibility.

Then STOP your turn and WAIT for the user's reply. The go-ahead must be an
explicit answer to THIS message -- the user's original "publish this" request
is NOT it, however specific it was. Never announce anything as "confirmed"
that the user has not themselves replied to: confirmation is something the
user gives, not something you declare. (A real publish run declared "include
set confirmed" and dispatched the worker in the same turn as its own
proposal; this gate exists to prevent exactly that.)

**A rename NEVER requires tearing down or relaunching the worker.** The
worker's own name and its branch name are internal plumbing -- they appear
nowhere in the published repo (§8 mints a single snapshot commit from the
final tree and pushes that commit, never the branch), so a stale name there
is irrelevant. If the user renames after
dispatch anyway, handle it in place:

- Renamed before the worker has run the script: just pass the new
  `--slug`/`--title` to `build_inspiration.sh`.
- Renamed after the script has run (worker mid-run or done): rename in
  place -- `git mv` the manifest and thumbnail to the new slug names, update
  the front-matter `title:` and the generated welcome's slug references,
  commit (in the worker's worktree). This preserves any FILL-IN prose and
  bespoke SVG already done. Do NOT re-run the script under a new slug in an
  already-assembled worktree: its carry-forward step would keep the
  old-slug files as if they were an accumulated earlier inspiration. A
  display-title-only change is just the front-matter edit.

## 2. Resolve `BASE_REF` (in-repo, no network)

`BASE_REF` is this workspace's **creation snapshot** -- the template state the
mind started from (or last updated itself to). Resolve it deterministically as
the NEWEST first-parent commit that is a template-state marker:

```bash
BASE_REF=$(git log --first-parent --format='%H %s' HEAD \
    | awk '{h=$1; sub(/^[^ ]+ /,""); if ($0 ~ /^update-self:/ || $0 == "Initial workspace commit") {print h; exit}}')
```

Two marker kinds; the newest one on the first-parent chain wins:

- **`update-self: ...`** -- the mind pulled a newer template version after
  creation (the same subject convention `update-self` / `assist` rely on).
- **`Initial workspace commit`** -- written by bootstrap on the mind's very
  first boot (always present -- it is created `--allow-empty` by
  `libs/bootstrap` -- and it snapshots exactly what the workspace started
  from, including any uncommitted source state a dev-flow clone carried).
  This is the normal answer for a mind that never ran `update-self`.

This is NOT a judgment call -- do not go hunting for an older "clean template"
commit past the marker. A full-history clone's first-parent ancestry reaches
ancient template commits that have nothing to do with this mind; the marker is
the mind's actual base.

**Fallback (only if NO marker exists** -- a hand-made or pre-bootstrap repo):
the **first-parent root**:

```bash
git rev-list --first-parent HEAD | tail -1
```

The fallback MUST be the first-parent root, never a bare root-commit lookup
(`git rev-list --max-parents=0 HEAD`): subtree merges add parallel root commits
that are NOT the seed (a mind repo can have several near-empty roots), while
the first-parent chain from HEAD always ends at the true template seed. Do NOT
fetch or pull from upstream to obtain `BASE_REF` in any case -- `parent.toml`
is a provenance link only.

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

Do NOT dispatch until the user has explicitly replied to §1's scope-gate
message confirming what goes in, what stays out, and the published-version
modifications. If a rename arrives after dispatch anyway, fix it in place per §1 --
never tear down or relaunch the worker for a rename (its name and branch are
internal and appear nowhere in the published repo).

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
`<description>`, `<BASE_REF>`, the include paths, and the user-confirmed
published-version modifications list (from §1's scope gate; write "None
requested." if there are none) into the body -- the worker must be able to
run the script verbatim, with zero back-and-forth:

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

2. **Apply the published-version modifications** (skip if the list below
   says none). These are user-confirmed edits that belong ONLY in the
   published snapshot -- the live mind keeps its own versions, and nothing
   you do here touches it:

   <one line per modification: file + the change to make, e.g.
   "libs/slack_zen_garden/config.py: replace the hardcoded '#team-garden'
   channel with a neutral default the adopter sets" -- or the single line
   "None requested.">

   After applying them, re-run the secret scan over every file you modified,
   with the same shared script the assembly's scan gate uses. It runs both
   scanners (betterleaks, kingfisher) and exits non-zero on any finding, any
   scanner error, or any missing scanner -- there is no fallback scanner:

   ```bash
   bash .agents/skills/publish-inspiration/scripts/scan_secrets.sh <each modified file>
   ```

   A finding means a modification did not fully remove a credential -- fix
   it or report `stuck`; never leave it in. If the script reports a missing
   scanner, or the script itself is absent from your assembled tree (a
   BASE_REF that predates it), report `stuck` -- do not substitute a weaker
   ad-hoc scan.

3. **Flesh out the manifest.** `inspiration-<slug>.md` at the repo root has
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

   "Prerequisites," "How to adapt it," and "Holes" must together realize the
   user-confirmed onboarding design from your task's Context: any data the
   app needs comes from the adopter's own connectors (initiated by the
   adopting agent via latchkey), never from a manual data-entry step where a
   connector exists, and every Hole is a genuine decision for the adopter --
   not a chore like "type your contacts into a file."

   The generated `README.md` at the repo root (the repo's GitHub landing
   page) carries ONE `<!-- FILL-IN (publishing agent): ... -->` block too --
   a short overview of this inspiration. Replace it with a GitHub-flavored
   version of the manifest's "What it is" (2-4 sentences). The rest of the
   README is generated correctly and describes this inspiration, not the
   template -- do not revert it to the default-workspace-template README.

4. **Design the thumbnail.** `inspiration-<slug>.svg` at the repo root is a
   generic placeholder the script generated -- it must never be published.
   Replace its entire contents with a bespoke SVG you design for THIS app: a
   clean, simple, iconic representation of what the app actually is and shows
   (derive it from the app code and the manifest you just wrote -- e.g. a
   stylized miniature of its main screen or its core object). Hard rules:
   mock data only, never real user data; no `<script>`; no `on*=` event
   attributes; no `<foreignObject>`; no external references (no href/src
   pointing outside the file) -- fully self-contained. Keep the root
   `viewBox` around 240x160.

5. **Commit** the modification + manifest + thumbnail edits as a follow-up
   commit on your branch (`mngr/<slug>`), in your worktree.

6. **Self-check, then report.** Both greps must print NOTHING before you may
   report `done`:

   ```bash
   grep -n -- '<!-- FILL-IN (publishing agent)' inspiration-<slug>.md README.md
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
- The USER-CONFIRMED onboarding design (write the manifest's Prerequisites /
  How to adapt it / Holes to MATCH this; adopter data comes from their own
  connectors via latchkey, initiated by the adopting agent -- never a manual
  data-entry step where a connector exists):
  <the lead's confirmed onboarding summary from §1: what the adopting agent
  self-initiates, what fills automatically, what the genuine Holes are>
- <extra context the lead has: what the app does for its user, known holes,
  tokens/accounts it depends on -- everything the worker needs to write a
  good manifest and a representative thumbnail>

## Success criteria

- `build_inspiration.sh` exited 0 and its commit is on `mngr/<slug>`.
- Every published-version modification applied, its files re-scanned clean.
- Every FILL-IN block replaced with real prose (or an explicit "none") -- in
  BOTH `inspiration-<slug>.md` and `README.md`.
- `README.md` describes this inspiration (not the default-workspace-template).
- The manifest's Prerequisites / How to adapt it / Holes match the
  user-confirmed onboarding design above (no manual data-entry steps where a
  connector exists; Holes are decisions, not chores).
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
  grep -n -- '<!-- FILL-IN (publishing agent)' "$WT/inspiration-<slug>.md" "$WT/README.md"
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

- **Secret scan (exit 1).** A credential/token rode in on an overlaid path,
  OR one of the two required scanners (betterleaks / kingfisher) was missing
  or errored -- the stderr says which. Nothing was committed; for a finding,
  surface the flagged path (value redacted) and stop. For a missing/broken
  scanner, the environment is broken (the binaries are baked into the
  workspace image; if one is missing, the stderr names the command to
  reinstall both) -- never publish around the gate.
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
root, not `/code`'s. (The preview-tab plumbing below is the one documented
exception -- see the CWD-invariant callout at the top of this skill.)

**Open the preview tab BEFORE sending the confirmation message.** The user
gets a rendered preview of exactly what will (and will not) be published, in
a tab named `inspiration-preview`, alongside the chat message. The preview is
ADDITIVE: it changes nothing about this section's hard-gate semantics -- the
explicit go-ahead still happens in chat, exactly as described below.

```bash
# 1. Re-publish safety: tear down any stale preview from an earlier run
#    (safe no-op when there is none). Same block as §10's teardown.
PREVIEW_STATE=/code/runtime/publish-inspiration/preview.env
if [ -f "$PREVIEW_STATE" ]; then
    . "$PREVIEW_STATE"
    if [ -n "${PREVIEW_PID:-}" ] && ps -p "$PREVIEW_PID" -o command= 2>/dev/null | grep -q http.server; then
        kill "$PREVIEW_PID"
    fi
    [ -n "${PREVIEW_DIR:-}" ] && rm -rf "$PREVIEW_DIR"
    rm -f "$PREVIEW_STATE"
fi
(cd /code && python3 scripts/forward_port.py --remove --name inspiration-preview)

# 2. Render the static preview page from the ASSEMBLED snapshot. Run the
#    script from /code's skill dir ($WT is reset to BASE_REF, which may
#    predate it); pass the SAME include/data-include paths you gave
#    build_inspiration.sh, and one --modification per applied
#    published-version modification (omit if none).
PREVIEW_DIR="$(mktemp -d)"
bash /code/.agents/skills/publish-inspiration/scripts/render_preview.sh \
    --worktree "$WT" \
    --slug <slug> \
    --out "$PREVIEW_DIR" \
    --include <path> [--include <path> ...] \
    [--data-include <path> ...] \
    [--modification "<text>" ...]

# 3. Serve it on a free localhost port as a BACKGROUND process and record the
#    PID + port + dir (the state file is what §10's teardown kills -- never a
#    pattern-based pkill).
PREVIEW_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
nohup python3 -m http.server "$PREVIEW_PORT" --directory "$PREVIEW_DIR" --bind 127.0.0.1 \
    > "$PREVIEW_DIR/server.log" 2>&1 &
PREVIEW_PID=$!
mkdir -p /code/runtime/publish-inspiration
printf 'PREVIEW_PID=%s\nPREVIEW_PORT=%s\nPREVIEW_DIR=%s\n' \
    "$PREVIEW_PID" "$PREVIEW_PORT" "$PREVIEW_DIR" > "$PREVIEW_STATE"
for _ in 1 2 3 4 5; do
    curl -sf "http://127.0.0.1:${PREVIEW_PORT}/" > /dev/null && break
    sleep 1
done
curl -sf "http://127.0.0.1:${PREVIEW_PORT}/" > /dev/null   # hard check: server is up

# 4. Register the tab. MUST run from /code -- the registry is the LIVE
#    mind's runtime/applications.toml, not $WT's.
(cd /code && python3 scripts/forward_port.py --name inspiration-preview --url "http://localhost:${PREVIEW_PORT}")
```

If the hard check in step 3 fails (the free port got taken in the window
before `http.server` bound it), kill the recorded PID and redo step 3 with a
fresh port. The server is a plain background process, so it survives across
your turns while you wait at the gate below.

Confirmation happens inline in chat -- there is no other confirmation
mechanism. Present the proposal to the user ONCE, in plain language:

- the **title** and **description**;
- the **repo name** (defaults to `slug`);
- the **visibility** (default: **private**);
- a short recap of the **published-version modifications** that were applied
  (or that there were none), so the user can verify their requested removals
  and changes actually happened;
- the **onboarding as actually written**: a two-or-three-line recap of the
  manifest's setup path and Holes as the worker wrote them (what an adopter's
  agent self-initiates, what fills automatically, what decisions remain), so
  the user confirms the REAL manifest -- not just the §1 design -- reads the
  way they approved. If the worker's version drifted from the confirmed
  design (e.g. it introduced a manual data-entry step), fix the manifest
  BEFORE presenting this message, not after;
- the **thumbnail** the sub-agent designed -- EMBED it in the chat message
  as a markdown image so the user actually sees what will represent their
  inspiration, using the file's absolute path:

  ```markdown
  ![<title> thumbnail]($WT/inspiration-<slug>.svg)
  ```

  (substitute the real absolute worktree path), and note you can adjust it if
  they'd like;
- the **preview tab**: tell the user the full preview is open in the
  `inspiration-preview` tab -- a page showing the title, thumbnail, manifest,
  and a plain breakdown of exactly what will and will not be published.
  (Keep embedding the thumbnail in chat as above; the tab is additive.)

Then END YOUR TURN and WAIT. **This is a hard gate, exactly like §1's:** §8
(create the repo + push) may only run after an explicit go-ahead in the
user's reply TO THIS MESSAGE. No earlier approval counts -- not the §1 scope
confirmation, not a "go ahead and publish" given before assembly, not
approving the GitHub permission requests in the minds app. The final title,
description, and thumbnail only came into existence during assembly, so the
user cannot have approved them yet. Your own gate checks (the FILL-IN /
placeholder / safety greps, generalization spot-checks) are VERIFICATION,
not confirmation -- they never substitute for the user's reply. (A real
publish run verified everything itself, announced "everything checks out,"
and pushed in the same turn -- the user never saw the thumbnail or final
details before the repo existed on their account. This gate exists to
prevent exactly that.)

Take edits in their replies and apply them; once their reply is an explicit
go-ahead, proceed with the agreed values. After applying confirmed
manifest/thumbnail edits, re-run step 2's `render_preview.sh` into the SAME
`$PREVIEW_DIR` so the tab reflects them on reload -- no server restart or
re-registration needed. Do not re-ask what they already
answered in §1; this is a confirm-and-adjust pass, not a second interview.
If the user asks to abort, stop here and leave the assembled commit intact
(§10's failure path) -- and run §10's preview teardown NOW: a stale preview
tab must never outlive the publish flow.

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

The flow needs TWO github scopes, both approved once by the user in minds:

- `github-rest-api` (`github-read-user` + `github-write-all`) -- the API
  calls in §8: repo creation and the topic. The names matter: repo creation
  is `POST /user/repos`, whose path the narrower `github-write-repos`
  permission does NOT match (its schema covers `/repos/...` paths only), and
  the `/user` probe needs `github-read-user`. Requesting narrower names
  produces a grant that 403s the flow's own calls even after the user
  approves.
- `github-git` (`github-git-write`) -- the `git push` itself. The gateway
  natively proxies GitHub's git smart-HTTP endpoints (a push is just a `GET
  info/refs?service=git-receive-pack` + a `POST git-receive-pack`), so the
  push goes through latchkey too; no token ever enters this container.

Probe both up front:

```bash
# API access. The -f matters: latchkey curl exits with curl's own code, and
# the gateway rejects unpermitted requests with an HTTP 403 (a completed
# exchange, so exit 0 without -f); -f turns a denial into exit 22:
latchkey curl -sf https://api.github.com/user
# Push access -- a github-git (or catch-all) rule granting github-git-write
# (or "any") must exist; grants can take either form, so check both:
latchkey curl http://latchkey-self.invalid/permissions/self \
    | jq -e '[.rules[]? | to_entries[] | select(.key == "github-git" or .key == "any") | select(any(.value[]?; . == "github-git-write" or . == "any"))] | length > 0' >/dev/null \
    && echo "git push: permitted" || echo "git push: NOT permitted"
```

For whichever is missing, initiate the permission request YOURSELF (each
request opens the approval/login flow in the minds app; the body must be
exactly the four fields shown -- `agent_id`, `type`, `payload`, `rationale`):

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "predefined", "payload": {"scope": "github-rest-api", "permissions": ["github-read-user", "github-write-all"]}, "rationale": "Publish this inspiration as a new GitHub repo on your account."}'
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "'"$MNGR_AGENT_ID"'", "type": "predefined", "payload": {"scope": "github-git", "permissions": ["github-git-write"]}, "rationale": "Push the published inspiration'"'"'s git history to the new repo."}'
```

Tell the user in chat that a GitHub approval is waiting for them in minds,
then poll the probes **as a background task, bounded** (mirror `launch-task`'s
background-await pattern; a foreground `while` loop can be killed by your own
tool-execution timeout):

```bash
# Run with Bash run_in_background: true -- bounded (~5 minutes), one wait, no re-arm thrash
for _ in $(seq 1 30); do
    if latchkey curl -sf https://api.github.com/user >/dev/null 2>&1 \
        && latchkey curl http://latchkey-self.invalid/permissions/self \
           | jq -e '[.rules[]? | to_entries[] | select(.key == "github-git" or .key == "any") | select(any(.value[]?; . == "github-git-write" or . == "any"))] | length > 0' >/dev/null; then
        echo "github access: permitted (api + git push)"
        exit 0
    fi
    sleep 10
done
echo "github access: still not permitted" >&2
exit 1
```

If the user never approves, surface a clear message and stop, leaving the
assembled commit intact -- and run §10's preview teardown (the preview tab
must not outlive the flow). Do NOT fall back to any other credential or
mechanism (no `GH_TOKEN`-in-URL pushes, no partial-tree API uploads -- see
the "MUST BE BOOTABLE" callout).

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
    -d '{"name": "<repo_name>", "description": "<description> (minds inspiration v1)", "private": <true|false>}'
```

Take `<owner>` from the response's `.owner.login`. `"private"` is `true` for
the default private visibility, `false` only if the user chose public. The
repo description is always the confirmed `<description>` followed by the
literal suffix ` (minds inspiration v1)` -- the flow-version marker every
published repo carries; keep it verbatim. You
already validated `repo_name` against `^[A-Za-z0-9._-]+$` in §6; keep the
JSON built from variables, never string-interpolated shell.

**Step 2 -- mint ONE snapshot commit and push it as `main` (git through the
latchkey gateway):**

The published history must be the public template's history plus EXACTLY ONE
snapshot commit. The worker's branch accumulates intermediate commits (the
raw assembly, then the modification/manifest/thumbnail follow-ups, then any
§6 edits) -- pushing the branch would publish every intermediate state, and
a published-version modification would leak the very thing it removed (a
real publish leaked a personal email exactly this way: it was generalized in
a follow-up commit, so the pre-cleanup assembly commit shipped too). So mint
a fresh commit from the FINAL tree, parented directly on `BASE_REF`, and
push that commit -- the branch itself is never pushed:

```bash
( cd "$WT" \
    && SNAPSHOT_COMMIT="$(git commit-tree 'HEAD^{tree}' -p <BASE_REF> -m "inspiration: <slug>

Assembled on clean DEFAULT_WORKSPACE_TEMPLATE base <BASE_REF> (provenance link only; no upstream fetch).")" \
    && git \
    -c "http.extraHeader=X-Latchkey-Gateway-Password: $LATCHKEY_GATEWAY_PASSWORD" \
    -c "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: $LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" \
    push "$LATCHKEY_GATEWAY/gateway/https://github.com/<owner>/<repo_name>.git" "${SNAPSHOT_COMMIT}:refs/heads/main" )
```

Pushing `<sha>:refs/heads/main` publishes only that one commit plus the base
history it is parented on, so the published tree is exactly `$WT`'s final
state and NO intermediate assembly state exists anywhere off this machine.
The gateway proxies git's smart-HTTP endpoints and injects the GitHub
credential server-side (gated by the `github-git-write` permission from §7);
the two extra headers are the gateway's own auth material, already in this
container's environment. The mind's own commit history never leaves the
machine either (`build_inspiration.sh` parents the assembly commit on
`BASE_REF`; the minted commit here is parented there directly). No GitHub
token appears anywhere -- not in the URL, not on disk -- and nothing is
written into git config or a named remote (the `-c` options apply to this
one command only; nothing to clean up afterward).

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

**Failure handling.** If the create fails, read the response body: a
`"request not permitted by the user"` error means the `github-rest-api`
grant is missing or too narrow -- go back to §7; a name-taken error means
asking in chat for a new name and retrying step 1. If the push fails,
diagnose before retrying step 2 -- do NOT re-create the repo:

- "request not permitted by the user" means the `github-git-write` permission
  is missing -- go back to §7.
- A request-body-too-large rejection (HTTP 413) means the user's minds app is
  older than the gateway's raised body cap and cannot proxy a push this size;
  report that plainly and stop.
- A rejection mentioning `workflow` scope means the stored GitHub credential
  cannot push `.github/workflows/` files (the template ships them); report it
  and stop rather than stripping files.

Keep the assembled commit intact in `$WT` throughout. For fixable causes,
fix and retry the failed step until it succeeds or the user aborts; for the
stop-and-report cases above, stop -- and whenever you stop or the user
aborts here, run §10's preview teardown before ending the flow.
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

**Preview teardown -- runs in EVERY exit from this flow**, success or abort
(user declined at §6, GitHub access never approved in §7, a stop-and-report
failure in §8): kill the recorded server PID, remove the tab registration,
and delete the rendered files. A stale `inspiration-preview` tab must never
outlive the publish flow. Kill ONLY the recorded PID (verified below) --
never a pattern-based `pkill`:

```bash
PREVIEW_STATE=/code/runtime/publish-inspiration/preview.env
if [ -f "$PREVIEW_STATE" ]; then
    . "$PREVIEW_STATE"
    if [ -n "${PREVIEW_PID:-}" ] && ps -p "$PREVIEW_PID" -o command= 2>/dev/null | grep -q http.server; then
        kill "$PREVIEW_PID"
    fi
    [ -n "${PREVIEW_DIR:-}" ] && rm -rf "$PREVIEW_DIR"
    rm -f "$PREVIEW_STATE"
fi
(cd /code && python3 scripts/forward_port.py --remove --name inspiration-preview)
```

(This is the same block §6 runs as its re-publish pre-clean, and it is part
of the CWD invariant's documented exception -- `forward_port.py` runs from
`/code` because the tab registry is the live mind's.)

On a successful push, clean up per `launch-task`'s conventions: the worker can
be destroyed now (destroying it removes its worktree, i.e. `$WT`), and the
local branch can go too -- the published snapshot commit was minted from the
branch's final tree and lives on the new remote (the branch's intermediate
commits were never pushed, by design):

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py destroy --name <slug>
git worktree prune
git branch -D "mngr/<slug>"
```

(No git remote cleanup is needed: §8 pushes to an explicit URL and never adds
a named remote.)

If the push failed and you are stopping (user aborted, unrecoverable error),
leave the worker, `$WT`, and the `mngr/<slug>` branch intact instead -- do not
delete work the user may want to retry or reassemble from. The preview
teardown above still runs: keeping the assembled work does not mean keeping
the preview tab.

Close the delegation step with a work-summary line. Report the new repo URL in
your final assistant message to the user (not in the step summary).

## The assembly script: `scripts/build_inspiration.sh`

The worker runs `scripts/build_inspiration.sh` from its worktree root (§3). It
is self-contained (the dev `create-new-mind-repo` recipe is NOT available in
the VM). Interface (cwd = worktree repo root):

```
.agents/skills/publish-inspiration/scripts/build_inspiration.sh \
  --base-ref <BASE_REF> \          # DEFAULT_WORKSPACE_TEMPLATE commit the mind was based on (provenance + clean base)
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
   commit/push). The scan is the sibling `scan_secrets.sh` over the staged
   overlay: TWO scanners -- betterleaks (configured by the sibling
   `betterleaks.toml`: its default ruleset plus the credential-filename
   blocklist and a broader Anthropic key rule) and kingfisher (always
   `--no-validate`) -- where a finding from EITHER of them, any scanner error,
   or any missing scanner binary fails the scan. There is no fallback scanner.
   This is the authoritative blocker, not LLM prose.
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

## The preview script: `scripts/render_preview.sh`

The LEAD runs `scripts/render_preview.sh` (never the worker) in §6, from
`/code`'s copy of this skill, against the assembled `$WT`. Interface:

```
.agents/skills/publish-inspiration/scripts/render_preview.sh \
  --worktree <path-to-$WT> \
  --slug <slug> \
  --out <dir> \                    # gets index.html + a copy of the thumbnail SVG
  [--include <path> ...] \         # the same paths passed to build_inspiration.sh
  [--data-include <path> ...] \    # ditto (labeled as opted-in data on the page)
  [--modification <text> ...]      # one per applied published-version modification
```

It renders a single static, fully self-contained page (inline CSS, no
external resources; the thumbnail is copied next to it and referenced
relatively). Every fact on the page is derived from `$WT`'s actual contents
-- the manifest front-matter and body, the thumbnail, `git ls-tree HEAD` --
never invented. The page carries a prominent PREVIEW banner (nothing
published yet; confirm in chat), a what-is-in-this-preview note (rendered
locally, mock-data thumbnail, nothing has left the machine), the
what-WILL-be-published breakdown (selected paths, generated files,
carried-forward inspirations, template base, and the one-snapshot-commit
history guarantee), and the what-is-NOT-published breakdown (chats /
transcripts / memory, secrets and `.env` files behind the two-scanner gate,
unselected apps and files, the mind's git history, uploads, and the
published-version modifications -- or "none requested"). Pass the include
paths explicitly; if omitted, the script derives them from the snapshot's
`inspiration: <slug>` assembly commit, and errors out if it cannot.
