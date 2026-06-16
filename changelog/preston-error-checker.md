- Added a new `error-watcher` background service. It scans every tmux window in
the agent's session every 5 seconds for output matching `/error|exception/i`
and, when a new match appears, sends a single batched message to a randomly
selected mngr agent so a service that errored gets noticed. It skips its own
window to avoid a feedback loop, alerts only on newly-appeared output (a static
error on screen is reported once), and quietly skips when no agent can currently
be messaged. The match pattern is overridable via the `ERROR_WATCHER_PATTERN`
environment variable.
