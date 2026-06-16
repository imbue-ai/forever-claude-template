- Added a new `error-watcher` background service. It scans every tmux window in
the agent's session every 5 seconds for output matching `/error|exception/i`
and, when a new match appears, sends a single batched message to a randomly
selected mngr agent so a service that errored gets noticed. It skips its own
window to avoid a feedback loop, alerts only on newly-appeared output (a static
error on screen is reported once), and quietly skips when no agent can currently
be messaged. The match pattern is overridable via the `ERROR_WATCHER_PATTERN`
environment variable.

- An error is now recorded as reported only after its alert is actually
delivered. If no agent can currently be messaged, or the send fails, the error
is no longer silently dropped: it stays eligible and is re-alerted on a later
poll once an agent becomes reachable.

- When the randomly chosen recipient cannot receive the alert (for example an
agent that stopped between listing and messaging), the watcher now falls back to
the other messageable agents in random order within the same poll instead of
giving up after one failed send.

- Alert recipients are now restricted to `type: claude` agents (in addition to
excluding `STOPPED` ones), so the non-interactive system-services agent is never
chosen as a recipient.

- Dedup now ignores volatile numbers in a matched line (timestamps, counters,
numeric ids collapse to `#`), so an error line that only changes its timestamp
each poll is reported once instead of triggering a fresh alert every 5 seconds.

- The watcher's in-memory dedup state is now bounded: it forgets the state for
windows that have closed and caps the keys kept per window, so the long-lived
service does not grow its memory without limit.

- More robust agent enumeration: if `mngr list` exits non-zero but still prints
a valid agent list (for example when one provider failed), the watcher now uses
that list instead of skipping the alert. Internally, commands that could not run
at all (missing binary, timeout) are now distinguished from a command that
genuinely exited with status 1.

- The watcher now also handles `SIGHUP` -- the signal the bootstrap manager
actually sends when it closes a service window -- so it shuts down cleanly on the
real stop path, and can be launched via `python -m error_watcher.watcher`.

- Fixed the timeout path so a partial payload a command printed before it hung is
actually preserved. On a timeout the command's output comes back as bytes (not
text), and the previous type check silently dropped it; the bytes are now decoded
and kept, so a still-valid agent list from a hung `mngr list` is no longer thrown
away.
