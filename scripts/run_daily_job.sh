#!/usr/bin/env bash
#
# run_daily_job.sh -- run a recurring job at its due hour, or the first minute
# the container is up after missing it.
#
# Invoked every minute by a /etc/cron.d line (through with_agent_env.sh). The
# rule: at most one run per INTERVAL_DAYS window (default 1: daily), and a run
# happens when the interval has elapsed since the last covered date AND (it is
# at or past DUE_HOUR, or the job is overdue past the interval -- then run
# immediately, whatever the hour).
#
# So for a daily job: up at 3 AM -> runs at 3 AM. Asleep at 3 AM, woken at
# 09:49 -> runs at 09:49. Woken at 00:30 the night after a successful run ->
# silent (nothing was missed). Woken at 00:30 after a fully missed day -> runs
# at 00:30. A weekly job (--interval-days 7) behaves identically over a 7-day
# window.
#
# The stamp is written before the job starts, so a failing job is retried on
# the next due day rather than every minute; failures are visible in the
# job's log. A missing stamp is treated conservatively: run only at/after
# DUE_HOUR, never in the small hours. The flock is held for the job's whole
# duration, so overlapping ticks skip.
#
# Usage: run_daily_job.sh <job-id> <due-hour> [--interval-days N] <command...>
set -euo pipefail

JOB_ID="$1"
DUE_HOUR="$2"
shift 2
INTERVAL_DAYS=1
if [ "${1:-}" = "--interval-days" ]; then
    INTERVAL_DAYS="$2"
    shift 2
fi

# Stamps live on the container rootfs (not under runtime/, so they are never
# backed up -- a recreated container starts fresh). The env override exists
# for tests.
STAMP_DIR="${MINDS_DAILY_STAMP_DIR:-/var/lib/minds/daily-stamps}"
STAMP="$STAMP_DIR/$JOB_ID"
mkdir -p "$STAMP_DIR"

# One invocation at a time per job; held until the job exits.
exec 9>"$STAMP.lock"
flock -n 9 || exit 0

today=$(date +%F)
hour=$((10#$(date +%H)))
last=$(cat "$STAMP" 2>/dev/null || echo "")

if [ -z "$last" ]; then
    # No stamp: conservative -- wait for the due hour, never the small hours.
    if [ "$hour" -ge "$DUE_HOUR" ]; then
        printf '%s\n' "$today" > "$STAMP"
        exec "$@"
    fi
    exit 0
fi

# Whole days since the last covered date. The half-day rounding slack keeps a
# DST-shortened or -lengthened day (23h/25h between local midnights) counting
# as exactly one day.
days_since=$(( ($(date -d "$today" +%s) - $(date -d "$last" +%s) + 43200) / 86400 ))
if [ "$days_since" -ge "$INTERVAL_DAYS" ]; then
    if [ "$hour" -ge "$DUE_HOUR" ] || [ "$days_since" -gt "$INTERVAL_DAYS" ]; then
        printf '%s\n' "$today" > "$STAMP"
        exec "$@"
    fi
fi
exit 0
