#!/usr/bin/env bash
# Assemble a clean, shareable "inspiration" snapshot on top of the FCT base the
# mind was created from, then commit it. Run directly by the publish-inspiration
# skill (no launch-task sub-agent) on an ISOLATED git worktree it creates in
# the same container (cwd = worktree repo root).
#
# The dev `create-new-mind-repo` recipe is NOT available in the VM, so this is
# self-contained. It does the assembly + secret scan + manifest/thumbnail +
# /welcome rewrite + boot smoke-check + single commit. It does NOT create the
# GitHub repo or push -- the lead owns the popup, GitHub login, and push.
#
# Known-correct methods embedded here (a prior build got these wrong):
#   - Clean base via `git read-tree -u --reset` + `git clean -fdxq`, NEVER
#     `git checkout <ref> -- .` (which leaks the mind's whole committed tree,
#     incl. secrets). No upstream fetch/pull -- provenance link only.
#   - Overlay via `rsync -a "$STAGE/" "$REPO/"` (root-to-root), NEVER
#     `cp -a "$STAGE/apps" "$REPO/apps"` (nests into apps/apps).
#   - Secret scan is a hard-failing (exit-non-zero, abort-before-commit) gate on
#     token patterns and credential filenames -- the authoritative blocker.
#   - Boot smoke-check via the supervisor python lib (realize/process_config),
#     NEVER `supervisord -t` (in supervisord, -t means --strip_ansi and LAUNCHES
#     the daemon).
#
# Exit codes: 0 = success; 1 = secret scan hit; 2 = usage error; 3 = nothing to
# publish beyond the base; 4 = boot smoke-check failed; 5 = --base-ref does not
# resolve to a bootable template tree.

set -euo pipefail

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

# A bootable base can still predate the /welcome inspiration-takeover markers
# (added in a later FCT commit than some older bootable bases), which would
# silently degrade step 8's welcome rewrite: the rewrite would be skipped, so
# a mind created from this inspiration would get the generic welcome instead
# of taking over into the adaptation conversation. Require the markers as
# exact whole lines in the base's welcome skill, the same way step 8 matches
# them, so a too-old base is caught here -- before any destructive read-tree --
# rather than surfacing as a benign-sounding warning after the commit.
WELCOME_CHECK_FILE=".agents/skills/welcome/SKILL.md"
welcome_missing=""
welcome_content="$(git show "${BASE_REF}:${WELCOME_CHECK_FILE}" 2> /dev/null || true)"
if [ -z "$welcome_content" ]; then
    welcome_missing="${WELCOME_CHECK_FILE} (not present in base tree)"
else
    if ! printf '%s\n' "$welcome_content" | grep -qxF -- '<!-- INSPIRATION:BEGIN -->'; then
        welcome_missing="${welcome_missing} <!-- INSPIRATION:BEGIN -->"
    fi
    if ! printf '%s\n' "$welcome_content" | grep -qxF -- '<!-- INSPIRATION:END -->'; then
        welcome_missing="${welcome_missing} <!-- INSPIRATION:END -->"
    fi
fi
if [ -n "$welcome_missing" ]; then
    echo "build_inspiration.sh: BASE REF INVALID: the tree of '${BASE_REF}' is missing the /welcome inspiration-takeover markers:${welcome_missing}" >&2
    echo "build_inspiration.sh: '${BASE_REF}' predates the welcome-takeover feature -- a mind created from this inspiration would not adapt on boot; walk forward along the first-parent chain to a newer base, or ask the user" >&2
    exit 5
fi

# --- 1. stage the selected paths out of the LIVE worktree BEFORE the reset ----

# rsync -R preserves each relative path so it lands at the same location under
# the stage dir; the reset in step 2 wipes the live paths, so we must capture
# them first. Also stage any pre-existing accumulated inspiration manifests +
# thumbnails so they carry forward (step 4).
STAGE="$(mktemp -d)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

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

# Token patterns and credential filenames. A hit prints the offending path (and
# a redacted marker for value hits) and exits non-zero so the worker reports
# `stuck` and NOTHING is committed or pushed. This is the enforced gate on top
# of the .gitignore denylist -- not LLM prose.

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

# 5a. credential filenames (basename or path-suffix match).
CREDENTIAL_BASENAMES=(
    ".git-credentials"
    ".netrc"
    ".claude.json"
    ".sesskey"
    ".pypirc"
)
# Path-suffix credential locations (not just basename).
CREDENTIAL_SUFFIXES=(
    ".config/gh/hosts.yml"
)
# Paths in ALL_FILES are already repo-root-relative (that is how they were
# overlaid), so they can be printed as-is.
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
TOKEN_PATTERN='ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|sk-ant-[A-Za-z0-9_-]{24,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
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
# publishing agent MUST replace before the popup/confirmation step.

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
---

# ${TITLE}

This file is the manifest for the **${TITLE}** inspiration (slug:
\`${SLUG}\`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

${manifest_description}

<!-- FILL-IN (publishing agent): BEFORE the popup step, replace this comment
with a one-paragraph overview of what this inspiration does for its user: the
problem it solves, the main things it produces (pages, reports, automations),
and what the user sees when it is running. Write for a reader who has never
seen the original mind. -->

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean forever-claude-template base):

${included_paths_block}
<!-- FILL-IN (publishing agent): BEFORE the popup step, replace this comment
with prose that makes the list above self-explanatory: for each included path,
say what it is (an app or lib with code, a skill, data) and what role it plays.
Then describe how the pieces wire together at runtime: which supervisord
programs (in supervisord.conf) run them, which ports they listen on and how
those are registered in forward_port.py (if applicable), and any scripts or
services that connect them. -->

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the \`use-inspiration\` skill's template path; in short:

1. Read this entire file first, especially "Holes" and "Permissions it may
   need" below -- they are your agenda for the conversation.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them.
3. Ask the user directly: "How do you want to adapt it?" Do not start changing
   anything before having this conversation.
4. Work through each hole interactively, one at a time. Translate each into
   plain language, ask for a decision only when you genuinely need one, and
   resolve the obvious ones yourself.
5. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

<!-- FILL-IN (publishing agent): BEFORE the popup step, replace this comment
with one bullet per hole: every part the adapter must supply or rewire --
stubbed integrations, hardcoded accounts/channels/ids, data that was not
included, anything that will not work out of the box. For each, say what is
missing and what a working replacement looks like. If there are genuinely no
holes, say so explicitly. -->

## Permissions it may need

<!-- FILL-IN (publishing agent): BEFORE the popup step, replace this comment
with the tokens, scopes, or external accounts the adapter must supply (for
example, an API token with a specific scope, or a Slack app installed in their
workspace). If none are needed, say so explicitly. -->

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
MANIFEST_EOF

# --- 7. generate a placeholder thumbnail (mock data only) --------------------

# A neutral placeholder SVG using MOCK data only -- never real user data. The
# lead may overwrite this with the popup-confirmed, server-sanitized SVG.
cat > "$THUMBNAIL" <<THUMB_EOF
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 160" role="img" aria-label="${TITLE}">
  <rect width="240" height="160" rx="12" fill="#1f2933"/>
  <rect x="20" y="24" width="200" height="20" rx="6" fill="#3e4c59"/>
  <rect x="20" y="60" width="140" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="84" width="180" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="108" width="100" height="12" rx="6" fill="#52606d"/>
  <text x="20" y="150" font-family="sans-serif" font-size="11" fill="#9aa5b1">inspiration</text>
</svg>
THUMB_EOF

# --- 8. rewrite the /welcome stable region -----------------------------------

# Replace everything BETWEEN the two markers (exclusive of the markers
# themselves) with the inspiration takeover region: the title, slug, one-line
# description, and manifest path, plus the instruction to take over the
# welcome (custom greeting instead of the generic one) and immediately start
# the adaptation conversation. Deterministic (awk on the two markers), never
# an LLM freeform edit. The markers and everything outside them (the generic
# welcome, the takeover instructions, the marker contract) are preserved.
# Markers are matched as EXACT WHOLE LINES (grep -x, awk string equality), so
# prose elsewhere in the file that merely mentions the marker text can never
# trigger the rewrite.
WELCOME_FILE=".agents/skills/welcome/SKILL.md"
if [ -f "$WELCOME_FILE" ] \
    && grep -qxF -- '<!-- INSPIRATION:BEGIN -->' "$WELCOME_FILE" \
    && grep -qxF -- '<!-- INSPIRATION:END -->' "$WELCOME_FILE"; then
    NEW_REGION_FILE="$(mktemp)"
    cat > "$NEW_REGION_FILE" <<WELCOME_REGION_EOF

This mind was created from an inspiration, so the **Inspiration takeover**
path of this skill applies -- NOT the generic welcome.

- Title: ${TITLE}
- Slug: \`${SLUG}\`
- Description: ${manifest_description}
- Manifest: \`inspiration-${SLUG}.md\` (at the repo root)

In your FIRST response: output a CUSTOM welcome that names **${TITLE}** and
gives the one-line description above, INSTEAD of the generic "Welcome to
Minds" message. Then, in the SAME turn and without waiting to be asked, read
\`inspiration-${SLUG}.md\` and begin the adaptation conversation by asking the
user how they want to adapt it -- the \`use-inspiration\` skill's template
path, with the manifest's "How to adapt it" section as the script.
WELCOME_REGION_EOF
    awk -v regionfile="$NEW_REGION_FILE" '
        $0 == "<!-- INSPIRATION:BEGIN -->" {
            print
            while ((getline line < regionfile) > 0) print line
            close(regionfile)
            skip = 1
            next
        }
        $0 == "<!-- INSPIRATION:END -->" {
            skip = 0
            print
            next
        }
        skip != 1 { print }
    ' "$WELCOME_FILE" > "${WELCOME_FILE}.tmp"
    mv "${WELCOME_FILE}.tmp" "$WELCOME_FILE"
    rm -f "$NEW_REGION_FILE"
else
    echo "build_inspiration.sh: warning: /welcome stable markers not found; skipping welcome rewrite" >&2
fi

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

# --- 10. single commit -------------------------------------------------------

# Record the provenance link to BASE_REF in the commit message. Do NOT add an
# upstream remote and do NOT fetch/pull -- parent.toml is a provenance link only.
git add -A
git commit -q -m "inspiration: ${SLUG}

Assembled on clean FCT base ${BASE_REF} (provenance link only; no upstream fetch)."

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
echo "  'What it is', 'How it works', 'Holes', and 'Permissions it may need' -- replace ALL of"
echo "  them with real content (or explicit 'none' prose) before opening the publish popup."
