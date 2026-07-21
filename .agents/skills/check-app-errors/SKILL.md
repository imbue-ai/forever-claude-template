---
name: check-app-errors
description: Survey the health of the supervised apps and services on this host and find errors in their logs. Use after building or editing any app/service to confirm it is actually running cleanly, when something seems broken, or when you want a quick read on what (if anything) is currently failing. The Caretaker agent reuses this as its log-scan step.
---

# Checking apps for errors

Background apps and services run under supervisord and write rotated logs to
`/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
This skill surveys those services and their logs to answer "is anything broken,
and if so, what and where?"

Be deliberate with your commands. A few targeted greps across all the logs at
once beat opening files one at a time -- it keeps the survey fast and cheap, which
matters because the Caretaker runs this routinely. Start broad (one grep over
every log), then narrow to the specific services and lines that look wrong.

## Step 1: Survey the services

```bash
supervisorctl status
```

This lists every supervised program and its state. Note anything that is not
`RUNNING`:

- `FATAL` / `BACKOFF` -- the program keeps crashing on startup.
- `EXITED` -- it stopped (expected for a one-shot like `deferred-install`, a
  problem for a long-lived daemon).
- `STARTING` for a long time -- it may be stuck.

The services in `RUNNING` may still be logging errors, so continue to the logs
even when everything looks up.

## Step 2: Scan the logs for errors

Run one broad search across every service's stderr first:

```bash
grep -nE 'Traceback|ERROR|Exception|CRITICAL' /var/log/supervisor/*-stderr.log
```

`-n` prints line numbers so you can jump straight to the context. Widen or
narrow from there as needed:

```bash
# include stdout (some apps log errors there too)
grep -nE 'Traceback|ERROR|Exception|CRITICAL|Fatal' /var/log/supervisor/*.log

# which logs changed most recently -- a crash usually just wrote to one
ls -lt /var/log/supervisor/

# focus on one service once you know which is misbehaving
tail -n 200 /var/log/supervisor/<name>-stderr.log

# follow a service live while you reproduce the problem
supervisorctl tail -f <name> stderr
```

When a grep hit lands inside a Python traceback, read a window around it (e.g.
`tail`, or open the file at the reported line) -- the final line of the traceback
names the actual exception, and the lines above it show where it came from.

## Step 3: Summarize

Report concisely:

- Which services are unhealthy (not `RUNNING`, or `RUNNING` but logging errors),
  and which are fine.
- For each problem: the error (e.g. the exception type and message), the log file
  and line where it appears, and the likely cause if it is clear from the trace.
- Where you did **not** find anything, so the reader knows the survey was
  thorough.

Keep the summary in plain terms and point at the exact log path for anything you
flag, so the next step (a fix, or a hand-off) starts from a precise location
rather than a re-scan.

## Notes

- `/var/log/supervisor/` logs are container-local and **not** backed up, so treat
  them as transient: capture anything you need to keep elsewhere.
- Logs rotate (`<name>-stderr.log.1`, `.2`, ...). If a recent error is not in the
  live file, check the rotated backups.
- To restart a crashed service after a fix, see the `update-service` skill
  (`supervisorctl restart <name>`).
