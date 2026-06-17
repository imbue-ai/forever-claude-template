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
   (`mngr list --format json` to enumerate, `mngr message` to send). Only
   `type: claude` agents that are not `STOPPED` are eligible -- this mirrors
   mngr's own deliverability rule and excludes the non-interactive
   system-services agent. If the chosen agent cannot receive the message, the
   watcher falls back to the other eligible agents within the same poll.

If multiple windows error in the same poll, one batched message covers them
all. If no agent can currently be messaged, the watcher logs the match and
skips sending without erroring -- and, because it only records an error as
reported once an alert is actually delivered, the still-visible error is
re-alerted on a later poll once an agent becomes reachable. The match pattern
can be overridden via the `ERROR_WATCHER_PATTERN` environment variable.

## Architecture

The watcher is split into three loosely-coupled layers so the *where errors come
from* and the *where alerts go* can each be replaced without touching the core
logic. `watcher.py` (the `main()` entry point) is just the wiring that picks one
concrete input and one concrete output and runs the poll loop.

- **Input layer (`inputs.py`).** `ErrorInput` is the abstract contract: `read()`
  returns an `ErrorReading` (an `origin` plus a list of `(name, content)`
  sources). `TmuxWindowErrorInput` is the only implementation today -- it wraps
  the tmux session-discovery and pane-capture work and excludes the watcher's own
  window. A different source (e.g. a systemd/journald reader) is a drop-in
  sibling that returns the same `ErrorReading`.

- **Routing layer (`routing.py`).** `ErrorRouter` is the main work and depends
  only on the two layer interfaces. It matches each source's content against the
  pattern, suppresses output it has already alerted on (per source, with
  number-insensitive dedup), and forwards genuinely-new matches to the output as
  one batched `ErrorAlert`. It records an error as alerted only after the output
  confirms delivery, so an undelivered alert is retried on a later poll.

- **Output layer (`outputs.py`).** `ErrorOutput` is the abstract contract:
  `deliver(alert)` returns a delivery id (the recipient) or `None`.
  `MngrAgentErrorOutput` implements delivery via the mngr CLI but leaves *which*
  agent(s) to target to an overridable `choose_recipients` method;
  `RandomMngrAgentErrorOutput` is the default uniform-random policy. Replacing the
  recipient choice later (e.g. routing to the agent best placed to fix the error)
  is a one-method subclass, with the delivery mechanics unchanged.

The two layers communicate only through the `ErrorReading` / `ErrorAlert` value
types and the `CommandRunner` seam (`commands.py`), which is the single point
where real subprocesses are run and is faked wholesale in tests.

## Non-goals

- **Deliberately naive matching.** Any line containing "error" or "exception"
  matches, including benign ones like `0 errors` or `ErrorBoundary`. v1 does no
  false-positive filtering.
- **No diagnosis or routing.** It does not classify, fix, or route the alert to
  a *relevant* agent; recipient selection is uniformly random (the source
  agent itself is eligible).
- **In-memory dedup only.** The already-alerted set is not persisted, so a
  restart may re-alert on errors still on screen.
- **Number-insensitive dedup.** Dedup keys ignore digit runs (timestamps,
  counters, numeric ids), so a re-stamped error line is reported once rather
  than every poll. The flip side is that two errors differing only in their
  numbers are treated as the same for alerting -- acceptable for a "something
  errored" nudge.
- **Visible pane only.** It scans the rendered pane (`capture-pane -p`), not
  scrollback or log files; errors that scroll past between polls may be missed.
