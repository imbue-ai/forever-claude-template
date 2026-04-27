#!/usr/bin/env bash
# Driver for crystallize-task: collapses ticket open + turn extract +
# task.md compose + mngr create + mngr push into one invocation. The
# lead-proxy poll stays outside (it requires the Bash tool's
# run_in_background flag, which a script cannot set for the lead).
#
# Usage:
#   dispatch.sh \
#       --slug <kebab-case-name> \
#       --task-body-file <path-to-body-md> \
#       [--source-artifacts-dir <dir>] \
#       [--turn-nth N]
#
# The body file is a markdown fragment WITHOUT frontmatter. The script
# prepends the standard frontmatter (lead_agent, lead_report_dir,
# transcript_path, plus source_artifacts_dir if provided) before
# concatenating the body.
#
# After successful dispatch, the script prints the exact bash one-liner
# the lead must run with run_in_background: true to start the
# lead-proxy poll. Capture that output, run it, and the worker is live.

set -euo pipefail

SLUG=""
BODY_FILE=""
SOURCE_ARTIFACTS_DIR=""
TURN_NTH="1"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --slug)
            SLUG="$2"; shift 2 ;;
        --task-body-file)
            BODY_FILE="$2"; shift 2 ;;
        --source-artifacts-dir)
            SOURCE_ARTIFACTS_DIR="$2"; shift 2 ;;
        --turn-nth)
            TURN_NTH="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,21p' "$0"; exit 0 ;;
        *)
            echo "dispatch.sh: unknown arg: $1" >&2
            exit 2 ;;
    esac
done

if [ -z "$SLUG" ] || [ -z "$BODY_FILE" ]; then
    echo "dispatch.sh: --slug and --task-body-file are required" >&2
    exit 2
fi
if [ ! -f "$BODY_FILE" ]; then
    echo "dispatch.sh: body file not found: $BODY_FILE" >&2
    exit 2
fi
if [ -n "$SOURCE_ARTIFACTS_DIR" ] && [ ! -d "$SOURCE_ARTIFACTS_DIR" ]; then
    echo "dispatch.sh: source-artifacts-dir not a directory: $SOURCE_ARTIFACTS_DIR" >&2
    exit 2
fi
if [ -z "${MNGR_AGENT_NAME:-}" ]; then
    echo "dispatch.sh: MNGR_AGENT_NAME is not set" >&2
    exit 2
fi

WORKER_NAME="crystallize-$SLUG"
RUNTIME_DIR="runtime/crystallize/$SLUG"
TASK_FILE="$RUNTIME_DIR/task.md"
TURN_FILE="$RUNTIME_DIR/turn.jsonl"
REPORT_FILE="$RUNTIME_DIR/reports/report.md"

# Step 1: tk ticket (best-effort).
if command -v tk >/dev/null 2>&1; then
    TK_OUTPUT=$(tk create "crystallize $SLUG" -t task \
        --acceptance "transcript extracted; task file written; worker launched; worker DONE; branch merged" 2>/dev/null || true)
    TICKET_ID=$(printf '%s\n' "$TK_OUTPUT" | tail -1 | tr -d '[:space:]')
    if [ -n "$TICKET_ID" ]; then
        tk start "$TICKET_ID" >/dev/null 2>&1 || true
    fi
fi

# Step 2: extract the turn.
mkdir -p "$RUNTIME_DIR"
uv run .agents/shared/scripts/extract_turn.py \
    --nth "$TURN_NTH" \
    --output "$TURN_FILE"

# Step 3: compose task.md from frontmatter + body file.
{
    echo "---"
    echo "lead_agent: $MNGR_AGENT_NAME"
    echo "lead_report_dir: $RUNTIME_DIR/reports/"
    echo "transcript_path: $TURN_FILE"
    if [ -n "$SOURCE_ARTIFACTS_DIR" ]; then
        # Strip trailing slash for consistency.
        echo "source_artifacts_dir: ${SOURCE_ARTIFACTS_DIR%/}/"
    fi
    echo "---"
    echo ""
    cat "$BODY_FILE"
} > "$TASK_FILE"

# Step 4: launch worker and push runtime dir.
mngr create "$WORKER_NAME" -t crystallize-worker \
    --label "workspace=${MINDS_WORKSPACE_NAME:-default}" \
    --message-file "$TASK_FILE"

mngr push "$WORKER_NAME:$RUNTIME_DIR/" \
    --source "$RUNTIME_DIR/" \
    --uncommitted-changes=merge

if [ -n "$SOURCE_ARTIFACTS_DIR" ]; then
    DEST="${SOURCE_ARTIFACTS_DIR%/}/"
    mngr push "$WORKER_NAME:$DEST" \
        --source "$DEST" \
        --uncommitted-changes=merge
fi

# Step 5: print the lead-proxy poll one-liner the lead must run as a
# Bash tool call with run_in_background: true. We cannot launch it
# from inside this script because that flag belongs to the harness.
cat <<EOF

dispatch.sh: worker $WORKER_NAME launched and runtime pushed.
Now run the following as a Bash tool call with run_in_background: true:

timeout 90m bash -c '
  while [ ! -f $REPORT_FILE ]; do sleep 10; done
  cat $REPORT_FILE
'

When a report arrives, follow .agents/shared/references/lead-proxy.md.
EOF
