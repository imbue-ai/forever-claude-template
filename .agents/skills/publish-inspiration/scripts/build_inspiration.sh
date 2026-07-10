#!/usr/bin/env bash
# Assemble a clean, shareable "inspiration" snapshot on top of the FCT base the
# mind was created from, then commit it. Run by the launch-task WORKER the
# publish-inspiration skill dispatches, from the worker's own git worktree
# (cwd = worktree repo root); the live mind's /code is never touched. This is
# v1 of the inspirations flow (see INSPIRATION_FLOW_VERSION below); the
# generated manifest records it as `format: v1` in its front-matter.
#
# The dev `create-new-mind-repo` recipe is NOT available in the VM, so this is
# self-contained. It does the assembly + secret scan + manifest/thumbnail +
# /welcome rewrite + boot smoke-check + single commit. It does NOT create the
# GitHub repo or push, and it deliberately leaves two things unfinished for the
# worker to complete before reporting done: the manifest's FILL-IN blocks (real
# prose) and the placeholder thumbnail (a bespoke, app-specific SVG). The lead
# owns the chat confirmation, GitHub login, and push.
#
# Known-correct methods embedded here (a prior build got these wrong):
#   - Clean base via `git read-tree -u --reset` + `git clean -fdxq`, NEVER
#     `git checkout <ref> -- .` (which leaks the mind's whole committed tree,
#     incl. secrets). No upstream fetch/pull -- provenance link only.
#   - Overlay via `rsync -a "$STAGE/" "$REPO/"` (root-to-root), NEVER
#     `cp -a "$STAGE/apps" "$REPO/apps"` (nests into apps/apps).
#   - Secret scan is a hard-failing (exit-non-zero, abort-before-commit) gate
#     -- the authoritative blocker. It runs gitleaks (with the sibling
#     gitleaks.toml config: the default ruleset plus the credential-filename
#     blocklist and a broader Anthropic key rule) when the binary is
#     installed (the deferred-install service provides it), and otherwise
#     falls back to a filename+grep scan over the same blocklist and token
#     patterns.
#   - Boot smoke-check via the supervisor python lib (realize/process_config),
#     NEVER `supervisord -t` (in supervisord, -t means --strip_ansi and LAUNCHES
#     the daemon).
#
# Exit codes: 0 = success; 1 = secret scan hit; 2 = usage error; 3 = nothing to
# publish beyond the base; 4 = boot smoke-check failed; 5 = --base-ref does not
# resolve to a bootable template tree.

set -euo pipefail

# Version of the inspirations flow (and of the manifest format this script
# writes into the generated manifest's `format:` front-matter key).
INSPIRATION_FLOW_VERSION="v1"

# Resolve this script's own directory up front, before any cd: the sibling
# gitleaks.toml config lives next to this script, and the script is invoked
# by a path that may be relative to the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- argument parsing --------------------------------------------------------

BASE_REF=""
SLUG=""
TITLE=""
DESCRIPTION=""
INCLUDE_PATHS=()
DATA_INCLUDE_PATHS=()

usage() {
    cat >&2 <<'USAGE'
Usage: build_inspiration.sh --base-ref <ref> --slug <slug> --title <title>
                            --include <path> [--include <path> ...]
                            [--data-include <path> ...] [--description <text>]
USAGE
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base-ref)
            BASE_REF="${2:-}"
            shift 2
            ;;
        --slug)
            SLUG="${2:-}"
            shift 2
            ;;
        --title)
            TITLE="${2:-}"
            shift 2
            ;;
        --description)
            DESCRIPTION="${2:-}"
            shift 2
            ;;
        --include)
            INCLUDE_PATHS+=("${2:-}")
            shift 2
            ;;
        --data-include)
            DATA_INCLUDE_PATHS+=("${2:-}")
            shift 2
            ;;
        -h | --help)
            usage
            ;;
        *)
            echo "build_inspiration.sh: unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [ -z "$BASE_REF" ] || [ -z "$SLUG" ] || [ -z "$TITLE" ]; then
    echo "build_inspiration.sh: --base-ref, --slug, and --title are required" >&2
    usage
fi
if [ "${#INCLUDE_PATHS[@]}" -eq 0 ]; then
    echo "build_inspiration.sh: at least one --include path is required" >&2
    usage
fi

# Validate the slug the same way the backend does: ^[A-Za-z0-9._-]+$ and no
# leading '-'. This names the manifest, thumbnail, and (via the caller) the repo.
if ! printf '%s' "$SLUG" | grep -Eq '^[A-Za-z0-9._-]+$' || case "$SLUG" in -*) true ;; *) false ;; esac; then
    echo "build_inspiration.sh: slug must match ^[A-Za-z0-9._-]+\$ and not start with '-': $SLUG" >&2
    exit 2
fi

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

MANIFEST="inspiration-${SLUG}.md"
THUMBNAIL="inspiration-${SLUG}.svg"

# --- 0. validate that BASE_REF is a real, bootable FCT template tree ---------

# Guard against a wrong --base-ref: minds assembled via subtree merges can have
# several parallel root commits, and a naive fallback can land on a near-empty
# one instead of the real FCT seed. Any bootable template tree must contain
# pyproject.toml and supervisord.conf, so require both in BASE_REF's tree. This
# runs BEFORE the destructive read-tree in step 2 so a bad ref aborts cleanly
# without touching the worktree.
if ! git rev-parse --verify --quiet "${BASE_REF}^{tree}" > /dev/null; then
    echo "build_inspiration.sh: BASE REF INVALID: '${BASE_REF}' does not resolve to a tree in this repo" >&2
    exit 5
fi
base_missing=""
for required in pyproject.toml supervisord.conf; do
    if [ -z "$(git ls-tree --name-only "${BASE_REF}^{tree}" -- "$required")" ]; then
        base_missing="${base_missing} ${required}"
    fi
done
if [ -n "$base_missing" ]; then
    echo "build_inspiration.sh: BASE REF INVALID: the tree of '${BASE_REF}' is missing:${base_missing}" >&2
    echo "build_inspiration.sh: '${BASE_REF}' does not look like a bootable forever-claude-template base (a wrong root commit from a subtree merge?) -- pass the real FCT seed commit as --base-ref" >&2
    exit 5
fi

# --- 1. stage the selected paths out of the LIVE worktree BEFORE the reset ----

# rsync -R preserves each relative path so it lands at the same location under
# the stage dir; the reset in step 2 wipes the live paths, so we must capture
# them first. Also stage any pre-existing accumulated inspiration manifests +
# thumbnails so they carry forward (step 4).
STAGE="$(mktemp -d)"
cleanup() {
    rm -rf "$STAGE"
    if [ -n "${GITLEAKS_CONFIG:-}" ]; then
        rm -f "$GITLEAKS_CONFIG"
    fi
}
trap cleanup EXIT

# Snapshot the gitleaks config OUT of the worktree before step 2's reset:
# BASE_REF may predate the config file, in which case the read-tree would
# delete it from the worktree before step 5's scan runs. Empty means "no
# config available" and step 5 uses the fallback scanner.
GITLEAKS_CONFIG=""
if [ -f "$SCRIPT_DIR/gitleaks.toml" ]; then
    GITLEAKS_CONFIG="$(mktemp)"
    cp "$SCRIPT_DIR/gitleaks.toml" "$GITLEAKS_CONFIG"
fi

stage_one() {
    # Stage a single repo-root-relative path if it exists in the live worktree.
    local rel="$1"
    if [ -e "$rel" ]; then
        rsync -aR "$rel" "$STAGE/"
    else
        echo "build_inspiration.sh: warning: include path not found, skipping: $rel" >&2
    fi
}

for rel in "${INCLUDE_PATHS[@]}"; do
    stage_one "$rel"
done
for rel in "${DATA_INCLUDE_PATHS[@]}"; do
    stage_one "$rel"
done

# Carry forward any existing accumulated inspirations (manifest + sibling svg).
shopt -s nullglob
for existing in inspiration-*.md inspiration-*.svg; do
    rsync -aR "$existing" "$STAGE/"
done
shopt -u nullglob

# --- 2. clean base = the FCT version the mind was based on --------------------

# read-tree -u --reset makes the index+worktree match BASE_REF, dropping
# tracked-but-not-in-base files. clean -fdxq then drops untracked AND gitignored
# cruft (secrets, runtime state). This is the ONLY correct way to get a clean
# base -- `git checkout <ref> -- .` would leave the mind's whole tree in place.
# NO fetch/pull: BASE_REF is already a real commit in this repo's history.
git read-tree -u --reset "$BASE_REF"
git clean -fdxq

# --- 3. overlay the staged paths onto the clean base -------------------------

# Root-to-root contents merge. The trailing slash on the source is load-bearing:
# it merges the stage's CONTENTS into $REPO, so a path like apps/foo lands at
# apps/foo even when apps/ already exists on the base -- never nesting apps/apps.
rsync -a "$STAGE/" "$REPO/"

# --- 4. (carry-forward already handled in step 1's staging) ------------------

# --- 5. secret scan (authoritative, hard-failing blocker) --------------------

# A hit prints the offending path (value redacted, never printed) and exits
# non-zero so the worker reports `stuck` and NOTHING is committed or pushed.
# This is the enforced gate on top of the .gitignore denylist -- not LLM
# prose. The scanner is gitleaks (installed post-boot by the deferred-install
# service; configured by the sibling gitleaks.toml: default ruleset +
# credential-filename blocklist + a broader Anthropic key rule) with a
# filename+grep fallback for containers where gitleaks is not (yet) installed.

scan_failed=0

# Files to scan: ONLY the content overlaid out of the live mind (the selected
# --include / --data-include paths, plus any carried-forward inspiration-*.md /
# .svg). The clean base is the trusted, public FCT template -- it cannot contain
# the user's secrets, and its own test fixtures legitimately hold placeholder
# token strings (e.g. "sk-ant-test"), so scanning it only produces false
# positives that block every publish. The real risk is a secret riding in from
# the live mind's overlaid paths, so that is exactly what we scan. Enumerating
# only the overlay also keeps the scan cheap regardless of how large the base is
# (never traverses vendor/, the base's fixtures, etc.).
#
# SCAN_ROOTS holds the repo-root-relative paths that were overlaid; ALL_FILES is
# every file under them that now exists in the assembled tree.
SCAN_ROOTS=()
for rel in "${INCLUDE_PATHS[@]}" "${DATA_INCLUDE_PATHS[@]}"; do
    [ -e "$rel" ] && SCAN_ROOTS+=("$rel")
done
shopt -s nullglob
for existing in inspiration-*.md inspiration-*.svg; do
    SCAN_ROOTS+=("$existing")
done
shopt -u nullglob

ALL_FILES=()
if [ "${#SCAN_ROOTS[@]}" -gt 0 ]; then
    mapfile -d '' ALL_FILES < <(find "${SCAN_ROOTS[@]}" -type f -print0)
fi

# Fallback scanner (5a + 5b): the historical filename+grep scan, used when
# gitleaks (or its config) is unavailable -- e.g. an older container where the
# deferred install has not run or finished -- or when gitleaks itself fails to
# run. It enforces the same credential-filename blocklist and token patterns
# that gitleaks.toml encodes.
run_fallback_secret_scan() {
    # 5a. credential filenames (basename or path-suffix match).
    local CREDENTIAL_BASENAMES=(
        ".git-credentials"
        ".netrc"
        ".claude.json"
        ".sesskey"
        ".pypirc"
    )
    # Path-suffix credential locations (not just basename).
    local CREDENTIAL_SUFFIXES=(
        ".config/gh/hosts.yml"
    )
    # Paths in ALL_FILES are already repo-root-relative (that is how they were
    # overlaid), so they can be printed as-is.
    local f base bad suffix hit
    for f in "${ALL_FILES[@]}"; do
        base="$(basename "$f")"
        for bad in "${CREDENTIAL_BASENAMES[@]}"; do
            if [ "$base" = "$bad" ]; then
                echo "build_inspiration.sh: SECRET SCAN FAILED: credential file present: ${f}" >&2
                scan_failed=1
            fi
        done
        for suffix in "${CREDENTIAL_SUFFIXES[@]}"; do
            case "$f" in
                *"$suffix")
                    echo "build_inspiration.sh: SECRET SCAN FAILED: credential file present: ${f}" >&2
                    scan_failed=1
                    ;;
            esac
        done
        # .env / .env.* (but not .env.example / .env.sample templates, which are
        # deliberately non-secret; a bare .env or .env.<anything-else> is blocked).
        case "$base" in
            .env | .env.*)
                case "$base" in
                    .env.example | .env.sample | .env.template) ;;
                    *)
                        echo "build_inspiration.sh: SECRET SCAN FAILED: env file present: ${f}" >&2
                        scan_failed=1
                        ;;
                esac
                ;;
        esac
    done

    # 5b. token / key value patterns inside file contents.
    # Patterns match the token PREFIX immediately followed by enough secret-body
    # characters to be a real credential, so short placeholder values that share a
    # prefix (e.g. "sk-ant-test", "ghp_example") do NOT fire:
    #   - GitHub PATs:    ghp_ / gho_ + 36 base62 chars; github_pat_ + 22+ base62/_.
    #   - Anthropic keys: sk-ant- + 24+ chars of [A-Za-z0-9-_] (real keys are ~90+;
    #                     "sk-ant-test" is only 4 trailing chars and is skipped).
    #   - AWS access ids: AKIA + 16 upper alnum.
    #   - PEM headers:    a private-key header line.
    local TOKEN_PATTERN='ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|sk-ant-[A-Za-z0-9_-]{24,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
    if [ "${#ALL_FILES[@]}" -gt 0 ]; then
        # -I skips binary files, -E enables the alternation, -l lists matching paths.
        # One grep over the overlaid file list (not a fork per file). Paths are
        # already repo-root-relative. grep exits 1 (no matches) or 2 (only
        # unreadable-file warnings) harmlessly; a real hit prints the path here.
        while IFS= read -r hit; do
            [ -n "$hit" ] || continue
            echo "build_inspiration.sh: SECRET SCAN FAILED: token/key pattern in: ${hit} (value redacted)" >&2
            scan_failed=1
        done < <(grep -IElE -- "$TOKEN_PATTERN" "${ALL_FILES[@]}" 2>/dev/null)
    fi
}

# Primary scanner: gitleaks over the STAGING dir, which contains exactly the
# overlaid files (the same set ALL_FILES enumerates in the assembled tree), so
# the trusted base is never traversed. --redact keeps secret values out of all
# output; findings are printed from the JSON report as rule+file, with staged
# paths mapped back to repo-root-relative ones (rsync -aR staged every file at
# its repo-relative path, so stripping the stage prefix recovers it).
run_gitleaks_secret_scan() {
    local report gitleaks_status
    report="$(mktemp)"
    gitleaks_status=0
    gitleaks dir "$STAGE" \
        --config "$GITLEAKS_CONFIG" \
        --redact \
        --no-banner \
        --report-format json \
        --report-path "$report" \
        || gitleaks_status=$?
    if [ "$gitleaks_status" -eq 1 ]; then
        # Exit 1 is gitleaks' "leaks found" code; anything else is an error.
        scan_failed=1
        if ! python3 - "$report" "$STAGE" <<'PYEOF'
import json
import os
import sys

report_path, stage = sys.argv[1], sys.argv[2]
with open(report_path) as fh:
    findings = json.load(fh)
stage_real = os.path.realpath(stage)
for finding in findings:
    path = finding.get("File", "")
    if os.path.isabs(path):
        # Map the staged path back to the repo-root-relative one. realpath on
        # both sides tolerates a symlinked temp dir (e.g. /var -> /private/var).
        path = os.path.realpath(path)
        for prefix in (stage_real + os.sep, stage + os.sep):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
    rule = finding.get("RuleID", "unknown-rule")
    print(
        f"build_inspiration.sh: SECRET SCAN FAILED: {rule}: {path} (value redacted)",
        file=sys.stderr,
    )
PYEOF
        then
            echo "build_inspiration.sh: SECRET SCAN FAILED (could not read the gitleaks report for details)" >&2
        fi
    elif [ "$gitleaks_status" -ne 0 ]; then
        # gitleaks itself broke (bad config, crash). Never skip the gate --
        # run the fallback scanner instead.
        echo "build_inspiration.sh: warning: gitleaks exited ${gitleaks_status} (not a findings exit); falling back to the filename+grep secret scanner" >&2
        run_fallback_secret_scan
    fi
    rm -f "$report"
}

if ! command -v gitleaks > /dev/null 2>&1; then
    echo "build_inspiration.sh: warning: gitleaks not installed (deferred install may not have finished); using the fallback filename+grep secret scanner" >&2
    run_fallback_secret_scan
elif [ -z "$GITLEAKS_CONFIG" ]; then
    echo "build_inspiration.sh: warning: gitleaks.toml not found next to this script; using the fallback filename+grep secret scanner" >&2
    run_fallback_secret_scan
else
    run_gitleaks_secret_scan
fi

if [ "$scan_failed" -ne 0 ]; then
    echo "build_inspiration.sh: aborting before commit -- secret scan found credentials or tokens in the assembled tree" >&2
    exit 1
fi

# --- no-diff guard: nothing to publish beyond the base -----------------------

# If the assembled tree is identical to BASE_REF's tree, there is nothing to
# publish. Compare via git: stage everything, then diff the index tree against
# BASE_REF's tree. (This runs before manifest/thumbnail/welcome writes, which
# would themselves create a diff.)
git add -A
ASSEMBLED_TREE="$(git write-tree)"
BASE_TREE="$(git rev-parse "${BASE_REF}^{tree}")"
if [ "$ASSEMBLED_TREE" = "$BASE_TREE" ]; then
    echo "build_inspiration.sh: nothing to publish -- the selected apps/features add nothing beyond the base" >&2
    exit 3
fi

# --- 6. generate the manifest ------------------------------------------------

# The manifest is the single document the NEXT agent (in a mind created from
# this inspiration) reads to understand, present, and adapt the inspiration.
# The deterministic parts (front-matter, included-path list, the "How to adapt
# it" script, section skeletons) are generated here; the prose that requires
# knowledge of the live mind is left as clearly-marked FILL-IN blocks that the
# worker MUST replace before reporting done.

# Human-readable list of what the snapshot includes, derived from the include
# paths (data includes are labeled as such).
included_paths_block=""
for rel in "${INCLUDE_PATHS[@]}"; do
    included_paths_block+="- \`${rel}\`"$'\n'
done
for rel in "${DATA_INCLUDE_PATHS[@]}"; do
    included_paths_block+="- \`${rel}\` (data, explicitly opted in)"$'\n'
done

manifest_description="$DESCRIPTION"
if [ -z "$manifest_description" ]; then
    manifest_description="A shareable snapshot of ${TITLE}."
fi

cat > "$MANIFEST" <<MANIFEST_EOF
---
title: ${TITLE}
description: ${manifest_description}
thumbnail: ${THUMBNAIL}
format: ${INSPIRATION_FLOW_VERSION}
---

# ${TITLE}

This file is the manifest for the **${TITLE}** inspiration (slug:
\`${SLUG}\`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

${manifest_description}

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with a one-paragraph overview of what this inspiration does for its user: the
problem it solves, the main things it produces (pages, reports, automations),
and what the user sees when it is running. Write for a reader who has never
seen the original mind. -->

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean forever-claude-template base):

${included_paths_block}
<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with prose that makes the list above self-explanatory: for each included path,
say what it is (an app or lib with code, a skill, data) and what role it plays.
Then describe how the pieces wire together at runtime: which supervisord
programs (in supervisord.conf) run them, which ports they listen on and how
those are registered in forward_port.py (if applicable), and any scripts or
services that connect them. -->

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by \`requires_\`):

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with one line per requirement, using exactly these forms:

- requires_permission: <latchkey scope> / <permission schema> (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- it must not merely mention it)
- requires_secret: <ENV_VAR or config key> (what it is for and where to put it)

Derive the real values from the included code (e.g. every service the app
calls through \`latchkey curl\`). Example:
- requires_permission: slack-api / slack-read-all (user-approved; adopting
  agent initiates during setup)
If nothing is required, write exactly: "No prerequisites -- runs with no
external permissions or secrets." -->

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the \`use-inspiration\` skill's template path; in short:

1. Read this entire file first, especially "Prerequisites" and "Holes"
   below -- Prerequisites are your SETUP agenda, Holes are your ADAPTATION
   agenda.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the Prerequisites).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every \`requires_permission\` line NOW
   via a latchkey permission request (see the \`latchkey\` skill; the request
   opens the approval/login flow in the minds app), wire up any
   \`requires_secret\` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each hole interactively, one at a time. Translate each into
   plain language, ask for a decision only when you genuinely need one, and
   resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with one bullet per hole: every ADAPTATION gap the adapter must decide or
rewire -- stubbed integrations, hardcoded accounts/channels/ids, data that was
not included, anything that will not work out of the box. For each, say what
is missing and what a working replacement looks like. Do NOT list activation
requirements here (permissions, tokens, accounts) -- those belong in
"Prerequisites" above. If there are genuinely no holes, say so explicitly. -->

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
MANIFEST_EOF

# --- 7. generate a placeholder thumbnail (mock data only) --------------------

# A neutral placeholder SVG using MOCK data only -- never real user data. The
# marker comment makes "placeholder still in place" a deterministic grep: the
# worker MUST replace this whole file with a bespoke, app-specific SVG before
# reporting done, and the lead's pre-push gate blocks on the marker.
cat > "$THUMBNAIL" <<THUMB_EOF
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 160" role="img" aria-label="${TITLE}">
  <!-- minds-placeholder-thumbnail: replace with a bespoke SVG before publishing -->
  <rect width="240" height="160" rx="12" fill="#1f2933"/>
  <rect x="20" y="24" width="200" height="20" rx="6" fill="#3e4c59"/>
  <rect x="20" y="60" width="140" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="84" width="180" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="108" width="100" height="12" rx="6" fill="#52606d"/>
  <text x="20" y="150" font-family="sans-serif" font-size="11" fill="#9aa5b1">inspiration</text>
</svg>
THUMB_EOF

# --- 8. write the inspiration-specific /welcome into the SNAPSHOT ------------

# The published repo ships its OWN welcome skill, generated here by overwriting
# .agents/skills/welcome/SKILL.md in the assembled tree. The TEMPLATE's welcome
# skill is deliberately untouched by the inspirations feature -- no marker
# region, no takeover branch; the inspiration handles changing the welcome
# entirely within the snapshot it publishes. Deterministic full-file write,
# never an LLM freeform edit; idempotent across accumulated publishes (each
# publish regenerates it targeting the newly-published slug, the latest).
WELCOME_FILE=".agents/skills/welcome/SKILL.md"
mkdir -p "$(dirname "$WELCOME_FILE")"
cat > "$WELCOME_FILE" <<WELCOME_EOF
---
name: welcome
description: Greet the user when a new project starts. This mind was created from the "${TITLE}" inspiration, so the welcome introduces that inspiration and immediately starts the adaptation conversation.
---

# Welcome the user (inspiration: ${TITLE})

This mind was created from an inspiration -- a published snapshot of apps
another mind built:

- Title: ${TITLE}
- Slug: \`${SLUG}\`
- Description: ${manifest_description}
- Manifest: \`inspiration-${SLUG}.md\` (at the repo root)

Do ALL of the following in your FIRST response, in the same turn, without
waiting to be asked:

1. Open with a short CUSTOM welcome that names **${TITLE}** and gives the
   one-line description above. Do NOT use a generic "Welcome to Minds"
   greeting and do NOT offer a generic suggestions list.
2. Immediately read \`inspiration-${SLUG}.md\` at the repo root (reading the
   manifest in the first turn is required).
3. In plain, non-technical language, present what the inspiration is and
   what it needs from the user -- name the manifest's "Prerequisites" (the
   connectors/permissions it runs on). Then ask whether they want to hook it
   up to their own accounts now (e.g. "Want me to connect this to your own
   Slack?"). End your first response on THAT question. This is the
   \`use-inspiration\` skill's template path; the manifest's "How to adapt
   it" section is the full script: if they say yes, ACTIVATE FIRST -- initiate
   each \`requires_permission\` via a latchkey permission request, get the
   app showing THEIR OWN DATA (that is the definition of working; a running
   service is not), invite them to take a look -- and only then ask how they
   want to adapt it.

If this repo has accumulated several \`inspiration-*.md\` manifests, the one
named above is the latest; treat the others as reference (they were likely
already adapted upstream).
WELCOME_EOF

# --- 9. boot smoke-check WITHOUT side effects, then single commit -------------

# Validate supervisord.conf via the supervisor python lib -- realize() +
# process_config() parse and check the config WITHOUT starting the daemon.
# NEVER `supervisord -t`: in supervisord, -t means --strip_ansi and LAUNCHES the
# daemon. If the lib is unavailable, skip the check (config holes in selected
# apps are acceptable; the base booting is what matters).
#
# Run the check with the interpreter that already ships the supervisor lib --
# the one behind the installed `supervisord` binary's shebang (system python) --
# NOT `uv run`. `uv run` would resolve and BUILD the whole project environment
# (workspace sources, native wheels like line-profiler) just to import one lib:
# many seconds on a cold clean base, and it can fail outright on an unrelated
# build error, spuriously aborting a publish that is otherwise fine. Deriving
# the interpreter from the supervisord shebang keeps the check ~0.1s and robust.
smoke_ok=1
if [ -f "supervisord.conf" ]; then
    SMOKE_PY="python3"
    SUPERVISORD_BIN="$(command -v supervisord 2>/dev/null || true)"
    if [ -n "$SUPERVISORD_BIN" ]; then
        shebang="$(head -1 "$SUPERVISORD_BIN" 2>/dev/null || true)"
        case "$shebang" in
            '#!'*)
                # First token after the "#!" is the interpreter path.
                candidate="$(printf '%s' "${shebang#\#!}" | awk '{print $1}')"
                [ -x "$candidate" ] && SMOKE_PY="$candidate"
                ;;
        esac
    fi
    if ! "$SMOKE_PY" - <<'PYEOF'
import sys

try:
    from supervisor.options import ServerOptions
except Exception:
    # supervisor lib unavailable in this interpreter -- skip the check.
    sys.exit(0)

options = ServerOptions()
options.configfile = "supervisord.conf"
options.realize(args=[])
options.process_config(do_usage=False)
PYEOF
    then
        smoke_ok=0
    fi
fi
if [ "$smoke_ok" -ne 1 ]; then
    echo "build_inspiration.sh: boot smoke-check FAILED -- supervisord.conf did not realize cleanly" >&2
    exit 4
fi

# --- 10. single commit, parented on BASE_REF (never on the mind's HEAD) ------

# The snapshot commit's parent is BASE_REF, NOT the branch's previous HEAD.
# This is a privacy invariant: the published repo's history must be the public
# template's history plus the snapshot commits -- never the mind's own commit
# history. Parenting on HEAD would ship every commit the mind ever made
# (including any secret that was ever committed and later removed: history
# keeps it retrievable), and would defeat published-version modifications
# ("publish a secret-cleaned copy of this file") entirely. commit-tree writes
# the already-validated assembled tree with the base as parent; reset --soft
# moves the branch there without touching the worktree or index.
git add -A
SNAPSHOT_COMMIT="$(git commit-tree "$(git write-tree)" -p "$BASE_REF" -m "inspiration: ${SLUG}

Assembled on clean FCT base ${BASE_REF} (provenance link only; no upstream fetch).")"
git reset --soft "$SNAPSHOT_COMMIT"

# --- 11. summary for the worker's done report --------------------------------

echo "build_inspiration.sh: assembled inspiration '${SLUG}' on clean base ${BASE_REF}"
echo "  included paths:"
for rel in "${INCLUDE_PATHS[@]}"; do
    echo "    - ${rel}"
done
if [ "${#DATA_INCLUDE_PATHS[@]}" -gt 0 ]; then
    echo "  data paths (opted in):"
    for rel in "${DATA_INCLUDE_PATHS[@]}"; do
        echo "    - ${rel}"
    done
fi
echo "  manifest:  ${MANIFEST}"
echo "  thumbnail: ${THUMBNAIL}"
echo "  boot smoke-check: passed"
echo "  NEXT: ${MANIFEST} still has <!-- FILL-IN (publishing agent): ... --> placeholders in"
echo "  'What it is', 'How it works', 'Prerequisites', and 'Holes', and ${THUMBNAIL}"
echo "  is a generic placeholder (marker comment inside). Replace ALL FILL-INs with real content"
echo "  (or explicit 'none' prose) AND replace the placeholder with a bespoke SVG for this app,"
echo "  then commit and self-check before reporting done."
