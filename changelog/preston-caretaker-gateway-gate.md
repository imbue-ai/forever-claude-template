Caretaker: defer the weekly check while the API gateway is unreachable.
The woken Caretaker agent's only route to the Claude API is the latchkey
gateway -- a reverse tunnel into the minds desktop app on the user's machine,
gone whenever the app is closed. Previously, if the weekly check fired while
the app was closed, it woke the agent anyway: the run died silently with
connection errors, and since the weekly stamp was already written, nothing
retried for a week. Now `scripts/caretaker_check.sh` verifies the gateway
answers HTTP at all (any response counts; only connection refused/timeout
means down) before doing anything that wakes the agent. If it is unreachable,
the check clears its daily-job stamp and exits, so the every-minute cron tick
simply retries until the app is open and the check runs the moment it can
actually succeed.
