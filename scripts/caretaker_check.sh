#!/usr/bin/env bash
#
# caretaker_check.sh -- the Caretaker's deterministic weekly check. The
# Caretaker is off by default: no cron entry exists until the enable-caretaker
# skill writes /etc/cron.d/minds-caretaker, whose every-minute tick runs
# run_daily_job.sh (job id caretaker, due hour 3, --interval-days 7), which
# execs this script when the weekly check is due. It wakes the Caretaker agent
# only when it finds something worth telling the user about -- handing the
# findings over via runtime/caretaker/findings.md -- or when the agent has
# never introduced itself (no runtime/caretaker/permissions.md yet).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CARETAKER_DIR="$ROOT/runtime/caretaker"

log() { printf '%s caretaker_check: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

mkdir -p "$CARETAKER_DIR"
MARKER="$CARETAKER_DIR/last_check"

# One-time introduction: the agent has never met the user (no permissions file
# yet), so wake it regardless of findings.
if [ ! -f "$CARETAKER_DIR/permissions.md" ]; then
    log "first run: waking the agent to introduce itself"
    touch "$MARKER"
    exec bash "$SCRIPT_DIR/run_schedule_agent.sh" caretaker --template caretaker
fi

FINDINGS=""
add_finding() { FINDINGS="${FINDINGS}- $1"$'\n'; }

# 1. Services in a bad state (FATAL: crashed and gave up; BACKOFF: crash-looping).
if command -v supervisorctl >/dev/null 2>&1; then
    bad_services=$(supervisorctl status 2>/dev/null | awk '$2 == "FATAL" || $2 == "BACKOFF" {print $1 " (" $2 ")"}' || true)
    [ -n "$bad_services" ] && add_finding "services in a bad state: $(echo "$bad_services" | tr '\n' ' ')"
fi

# 2. Service logs with fresh error output since the last check (falls back to
# the last 7 days on the first post-introduction check, before a marker exists).
if [ -d /var/log/supervisor ]; then
    if [ -f "$MARKER" ]; then newer=(-newer "$MARKER"); else newer=(-mtime -7); fi
    error_logs=$(find /var/log/supervisor -name '*stderr*' "${newer[@]}" -size +0c 2>/dev/null \
        | xargs -r grep -liE 'error|traceback|exception' 2>/dev/null || true)
    [ -n "$error_logs" ] && add_finding "fresh error output in: $(echo "$error_logs" | tr '\n' ' ')"
fi

# 3. Disk nearly full. The env override exists for tests.
disk_threshold="${MINDS_CARETAKER_DISK_THRESHOLD:-85}"
disk_used=$(df -P / | awk 'NR==2 {gsub("%",""); print $5}')
[ "$disk_used" -ge "$disk_threshold" ] && add_finding "disk is ${disk_used}% full"

# 4. The OOM guard shed processes since the last check.
SHED="$ROOT/runtime/oom_priority/events/shed.jsonl"
if [ -f "$SHED" ] && [ -s "$SHED" ] && { [ ! -f "$MARKER" ] || [ "$SHED" -nt "$MARKER" ]; }; then
    add_finding "the memory guard had to stop processes (see runtime/oom_priority/events/shed.jsonl)"
fi

touch "$MARKER"
if [ -z "$FINDINGS" ]; then
    log "all clear; nothing to report until the next check"
    exit 0
fi

log "findings; waking the agent"
{
    printf '# What the weekly check found (%s)\n\n' "$(date +'%Y-%m-%d %H:%M %Z')"
    printf '%s' "$FINDINGS"
} > "$CARETAKER_DIR/findings.md"
exec bash "$SCRIPT_DIR/run_schedule_agent.sh" caretaker --template caretaker
