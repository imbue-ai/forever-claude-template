#!/usr/bin/env bash
# PreToolUse hook: when the agent is about to run `tk close` and it has
# emitted user-facing prose since its last tool call, that prose will be
# stranded INSIDE the closing step under the progress view's reply rule
# (the backward reply scan stops at the `closed` event, so a message that
# precedes the close is not promoted to a top-level reply). This injects a
# non-blocking reminder (exit 0 with stdout) telling the agent to re-output
# that text AFTER the close if it was meant for the user.
#
# It mirrors the claude_require_steps_pretool.sh / claude_open_tickets_*.sh
# pattern: a hook script wired in .claude/settings.json, not logic inside tk
# (tk has no access to the conversation transcript). Lives as a hook because
# only the Claude Code harness exposes the transcript_path.
set -euo pipefail

input=$(cat)

# Subagents manage their own progress view; don't nudge them.
[[ -z "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]] || exit 0

tool_name=$(echo "$input" | jq -r '.tool_name // empty')
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$input" | jq -r '.tool_input.command // empty')
# Only fire for `tk close` / `ticket close` invocations (optionally with a
# path prefix or leading env assignments).
if [[ ! "$command" =~ (^|[^[:alnum:]_])(tk|ticket)[[:space:]]+close([[:space:]]|$) ]]; then
    exit 0
fi

transcript_path=$(echo "$input" | jq -r '.transcript_path // empty')
[[ -n "$transcript_path" && -f "$transcript_path" ]] || exit 0

# Walk the transcript (JSONL) to decide whether user-facing text was emitted
# since the last tool call. Build a per-message {tool, text} stream, then
# scan from the end: skip the imminent trailing run of tool-only messages
# (the about-to-run `tk close` and any batched calls, which may or may not be
# in the transcript yet at PreToolUse time), then look back -- any text-only
# message before the next-older tool call means there is dangling prose.
dangling=$(jq -s '
  [ .[]
    | select(.type == "assistant" or .type == "user")
    | (.message.content) as $c
    | select(($c | type) == "array")
    | {
        tool: ($c | any(.[]; .type == "tool_use" or .type == "tool_result")),
        text: ($c | map(select(.type == "text") | .text) | join("") | gsub("^\\s+|\\s+$"; ""))
      }
  ]
  | reverse
  | reduce .[] as $r ({skipping: true, stop: false, dangling: false};
      if .stop then .
      elif .skipping and $r.tool then .
      else
        .skipping = false
        | if $r.tool then .stop = true
          elif ($r.text | length > 0) then .dangling = true
          else . end
      end)
  | .dangling
' "$transcript_path" 2>/dev/null || echo "false")

if [[ "$dangling" == "true" ]]; then
    cat <<'EOF'

[Progress-view reminder]

You wrote user-facing text *before* this `tk close`. The chat progress view detects your reply by scanning backward from the end of the turn and stopping at the first closed step -- so a message written before a close stays buried inside the step (the user only sees it by expanding that step), not as your top-level reply.

If that text was a general/user-facing message (a wrap-up, answer, or question), re-output it now AFTER this close so it renders as your reply below the timeline. If it was only internal/mid-work narration, ignore this.

EOF
fi
exit 0
