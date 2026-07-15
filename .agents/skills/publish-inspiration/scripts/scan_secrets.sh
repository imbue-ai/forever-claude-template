#!/usr/bin/env bash
# Hard-failing secret scan over explicit paths, shared by the
# publish-inspiration flow: build_inspiration.sh (section 5) runs it over the
# staged overlay, and the assembly worker re-runs it over any
# published-version-modified files.
#
# The scan runs TWO independent scanners over the same targets, BOTH of which
# must pass -- a finding from EITHER of them fails the scan:
#
#   - betterleaks: `betterleaks dir` with the sibling betterleaks.toml config
#     (default ruleset + credential-filename path rules + a broad Anthropic
#     key rule). Live validation of findings is off by default and never
#     enabled here.
#   - kingfisher: `kingfisher scan` ALWAYS with --no-validate -- live
#     validation would send candidate secrets to third-party APIs, which must
#     never happen with scanned content.
#
# There is NO fallback scanner and NO tolerance for a missing or broken tool:
# a missing binary or a scanner that errors at runtime fails the scan (a
# broken scanner must never silently pass). The two binaries are baked into
# the workspace image at build time; if one is missing, running
# scripts/install_secret_scanners.sh (the single source of truth for the
# version pins) installs both.
#
# Output: one line per finding on stderr naming the scanner, rule/detector,
# and path -- secret VALUES are never printed (betterleaks and kingfisher both
# redact their reports). Finding paths under a scanned directory target are
# printed relative to that directory: the stage dir holds files at their
# repo-relative locations, so findings print repo-relative.
#
# Usage: scan_secrets.sh [--config <betterleaks-toml>] <path> [<path> ...]
# Exit codes: 0 = both scanners ran and found nothing;
#             1 = findings, or a scanner missing/errored, or a target missing;
#             2 = usage error.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- argument parsing --------------------------------------------------------

CONFIG="$SCRIPT_DIR/betterleaks.toml"
TARGETS=()

usage() {
    echo "Usage: scan_secrets.sh [--config <betterleaks-toml>] <path> [<path> ...]" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --config)
            CONFIG="${2:-}"
            shift 2
            ;;
        -h | --help)
            usage
            ;;
        *)
            TARGETS+=("$1")
            shift
            ;;
    esac
done

if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "scan_secrets.sh: at least one target path is required" >&2
    usage
fi

# --- preconditions: every target, every scanner, the config -------------------

# All hard requirements are checked up front and reported together, so one
# message names everything that is broken. A missing target would otherwise
# scan as silently clean (betterleaks scans 0 bytes and exits 0), and a
# missing scanner must abort rather than weaken the gate to two tools.
precondition_failed=0
for target in "${TARGETS[@]}"; do
    if [ ! -e "$target" ]; then
        echo "scan_secrets.sh: TARGET MISSING: $target does not exist (refusing to scan-as-clean)" >&2
        precondition_failed=1
    fi
done
for tool in betterleaks kingfisher; do
    if ! command -v "$tool" > /dev/null 2>&1; then
        echo "scan_secrets.sh: SCANNER MISSING: '$tool' is not installed. The secret scanners are baked into the workspace image; if one is missing, install both by running: bash scripts/install_secret_scanners.sh (from the repo root). Refusing to scan without it." >&2
        precondition_failed=1
    fi
done
if [ ! -f "$CONFIG" ]; then
    echo "scan_secrets.sh: CONFIG MISSING: betterleaks config not found at $CONFIG" >&2
    precondition_failed=1
fi
if [ "$precondition_failed" -ne 0 ]; then
    exit 1
fi

scan_failed=0

# Print findings from a scanner's report file (stderr, one line per finding,
# no secret values). $1 names the report format: betterleaks (JSON array with
# RuleID/File) or kingfisher (JSON lines with rule.name + finding.path).
# Remaining args: report path, then the scan targets (finding paths under a
# directory target print relative to it).
_print_findings() {
    if ! python3 - "$@" << 'PYEOF'
import json
import os
import sys

fmt, report_path = sys.argv[1], sys.argv[2]
targets = sys.argv[3:]
dir_targets = [os.path.realpath(t) for t in targets if os.path.isdir(t)]
file_targets = {os.path.realpath(t): t for t in targets if not os.path.isdir(t)}


def relativize(path: str) -> str:
    real = os.path.realpath(path)
    for dir_target in dir_targets:
        prefix = dir_target + os.sep
        if real.startswith(prefix):
            return real[len(prefix):]
    # A file passed directly as a target prints exactly as it was passed
    # (the worker passes repo-relative paths).
    return file_targets.get(real, path)


def emit(rule: str, path: str, line: object) -> None:
    location = relativize(path) if path else "<unknown path>"
    if isinstance(line, int) and line > 0:
        location = f"{location}:{line}"
    print(
        f"scan_secrets.sh: SECRET SCAN FINDING [{fmt}] {rule}: {location} (value redacted)",
        file=sys.stderr,
    )


with open(report_path) as fh:
    if fmt == "betterleaks":
        for finding in json.load(fh) or []:
            emit(finding.get("RuleID", "unknown-rule"), finding.get("File", ""), finding.get("StartLine"))
    elif fmt == "kingfisher":
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            entry = json.loads(raw_line)
            if "rule" not in entry or "finding" not in entry:
                continue  # the trailing scan-summary line
            emit(entry["rule"].get("name", "unknown-rule"), entry["finding"].get("path", ""), entry["finding"].get("line"))
    else:
        raise ValueError(f"unknown report format: {fmt}")
PYEOF
    then
        echo "scan_secrets.sh: SECRET SCAN FAILED ($1 found leaks but its report could not be parsed for details)" >&2
    fi
}

# Report a scanner that broke at runtime (any exit code outside its documented
# clean/findings codes): print its captured stderr and fail the scan -- the
# gate never passes on a broken tool.
_scanner_error() {
    local tool="$1" rc="$2" errlog="$3"
    scan_failed=1
    echo "scan_secrets.sh: SCANNER ERROR: $tool exited $rc (not a clean/findings exit); failing the scan" >&2
    cat "$errlog" >&2 || true
}

# --- scanner 1: betterleaks ---------------------------------------------------

# Exit codes: 0 = clean; 99 = leaks (--exit-code overrides the default 1,
# which betterleaks also uses for fatal errors like a bad config); anything
# else = error. --redact keeps secret values out of the JSON report.
run_betterleaks() {
    local report errlog rc=0
    report="$(mktemp)"
    errlog="$(mktemp)"
    betterleaks dir "${TARGETS[@]}" \
        --config "$CONFIG" \
        --redact \
        --no-banner \
        --exit-code 99 \
        --report-format json \
        --report-path "$report" \
        > /dev/null 2> "$errlog" || rc=$?
    if [ "$rc" -eq 99 ]; then
        scan_failed=1
        _print_findings betterleaks "$report" "${TARGETS[@]}"
    elif [ "$rc" -ne 0 ]; then
        _scanner_error betterleaks "$rc" "$errlog"
    fi
    rm -f "$report" "$errlog"
}

# --- scanner 2: kingfisher ----------------------------------------------------

# Exit codes: 0 = clean; 200 = findings; 205 = validated findings (cannot
# happen with --no-validate, but treated as findings for safety); anything
# else = error. --no-validate is REQUIRED (see the header); --redact replaces
# secret values with hashes in the JSONL report.
run_kingfisher() {
    local report errlog rc=0
    report="$(mktemp)"
    errlog="$(mktemp)"
    kingfisher scan "${TARGETS[@]}" \
        --no-validate \
        --no-update-check \
        --redact \
        --format jsonl \
        --quiet \
        > "$report" 2> "$errlog" || rc=$?
    if [ "$rc" -eq 200 ] || [ "$rc" -eq 205 ]; then
        scan_failed=1
        _print_findings kingfisher "$report" "${TARGETS[@]}"
    elif [ "$rc" -ne 0 ]; then
        _scanner_error kingfisher "$rc" "$errlog"
    fi
    rm -f "$report" "$errlog"
}

# Run both even after one fails, so a single run reports every scanner's
# findings; the combined verdict is the OR of both of them.
run_betterleaks
run_kingfisher

if [ "$scan_failed" -ne 0 ]; then
    exit 1
fi
echo "scan_secrets.sh: clean -- both scanners (betterleaks, kingfisher) found nothing" >&2
exit 0
