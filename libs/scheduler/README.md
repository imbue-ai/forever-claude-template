# scheduler

A thin, file-driven task scheduler for the agent workspace. It runs as a
supervised background service and executes recurring shell commands on a cron
schedule, with one property plain cron lacks: **offline catch-up**. If the
machine was off when a task was due, the task runs once shortly after the next
boot (multiple missed intervals coalesce into a single run).

## Why not cron?

`cron` (and `mngr schedule`, which sits on top of it) never re-runs jobs that
were missed while the machine was off, and the schedule is split across the
crontab plus scattered state. This service instead owns one human- and
agent-readable file and tracks each task's last run, so it can catch up on boot.

## Files (all under `runtime/`, shared by every agent on the host)

- `runtime/scheduled_tasks.toml` -- the schedule. One `[[task]]` table per task:

  ```toml
  [[task]]
  name = "caretaker"
  schedule = "0 3 * * *"                # standard 5-field cron
  command = "bash scripts/run_task_agent.sh caretaker --template caretaker"
  enabled = true
  catch_up = true                       # run once on boot if missed during downtime
  description = "Nightly Caretaker run."
  ```

- `runtime/scheduler/state.toml` -- per-task last-run state (so catch-up survives reboots).
- `runtime/scheduler/timezone` -- single line IANA tz name (e.g. `America/New_York`); absent -> host clock.
- `runtime/scheduler/logs/<name>.log` -- captured output of each task run.

## Usage

The daemon is run by supervisord:

```bash
uv run scheduler run
```

Agents manage the schedule through the same console script (see the
`manage-scheduled-tasks` skill):

```bash
scheduler list                 # show current tasks
scheduler add --name backup --schedule "0 * * * *" --command "..."
scheduler show backup
scheduler remove backup
```

## Catch-up semantics

A task is **due** when its most recent scheduled fire time is strictly after its
recorded `last_run_at`. Because only the single most recent fire time is
considered, several intervals missed during downtime collapse into one run. A
task with `catch_up = false` only runs if that fire time is within the current
tick (a stale missed fire is skipped). A newly seen task is "armed" (its
`last_run_at` is set to now without running) so adding a task never triggers an
immediate run -- it first fires at its next scheduled time.
