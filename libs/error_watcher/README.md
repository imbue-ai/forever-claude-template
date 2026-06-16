# error_watcher

Background service that scans every tmux window in its session for output
matching `/error|exception/i` and, on newly-appeared matches, nudges a human-
facing agent so a service that errored doesn't scroll past unnoticed.

It is registered as `[services.error-watcher]` in `services.toml`, so the
bootstrap service manager runs it in its own `svc-error-watcher` tmux window
alongside the other services.

## What it does

On a fixed 5-second cadence (matching the bootstrap manager) the watcher:

1. Discovers its own tmux session (`tmux display-message -p '#S'`).
2. Enumerates every window in the session and captures each one's visible pane
   text, **excluding its own `svc-error-watcher` window** so its own alert text
   (which contains the word "error") cannot re-trigger a match.
3. Flags any window whose pane contains a line matching `/error|exception/i`
   (case-insensitive).
4. For genuinely new matches only (it remembers what it has already alerted on,
   per window), sends a single batched message naming each offending window and
   its matching line(s) to one randomly selected messageable mngr agent
   (`mngr list --format json` to enumerate, `mngr message` to send).

If multiple windows error in the same poll, one batched message covers them
all. If no agent can currently be messaged (every agent is `STOPPED`), the
watcher logs the match and skips sending without erroring. The match pattern
can be overridden via the `ERROR_WATCHER_PATTERN` environment variable.

## Non-goals

- **Deliberately naive matching.** Any line containing "error" or "exception"
  matches, including benign ones like `0 errors` or `ErrorBoundary`. v1 does no
  false-positive filtering.
- **No diagnosis or routing.** It does not classify, fix, or route the alert to
  a *relevant* agent; recipient selection is uniformly random (the source
  agent itself is eligible).
- **In-memory dedup only.** The already-alerted set is not persisted, so a
  restart may re-alert on errors still on screen.
- **Visible pane only.** It scans the rendered pane (`capture-pane -p`), not
  scrollback or log files; errors that scroll past between polls may be missed.
