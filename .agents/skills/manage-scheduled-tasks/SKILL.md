---
name: manage-scheduled-tasks
description: Query and edit the recurring scheduled tasks that run on this host. Use when you (or the user, via you) want to see what is scheduled, add a new recurring task, change when something runs, or stop a task from running. Tasks live in runtime/scheduled_tasks.toml and are driven by the scheduler service.
---

# Managing scheduled tasks

A file-driven scheduler service runs recurring tasks on this host. The schedule
lives in `runtime/scheduled_tasks.toml` -- a single, human- and agent-readable
list of what runs and when. The `scheduler` service reads it once a minute and
runs anything that is due, so **edits take effect within about a minute with no
restart**.

Prefer the `scheduler` CLI over hand-editing the file: the service may be writing
to the file at the same moment you are, and the CLI validates fields and
preserves the file's formatting and comments. Only fall back to editing the file
directly if you need to do something the CLI cannot express, and even then expect
the service to keep running underneath you.

## Look at what is scheduled

```bash
scheduler list                 # human-readable table of all tasks
scheduler list --format json   # same data as JSON, for scripting
scheduler show <name>          # full detail for one task
```

## Add a task

```bash
scheduler add \
    --name backup-notes \
    --schedule "0 3 * * *" \
    --command "bash scripts/backup_notes.sh"
```

`--name`, `--schedule`, and `--command` are required. Optional flags:

- `--description "<text>"` -- a one-line explanation of what the task does and
  why. Always include one; it is what a future reader (or the user) sees in
  `scheduler list`.
- `--disabled` -- add the task but leave it switched off (`enabled = false`), so
  it does not run until re-enabled.
- `--no-catch-up` -- turn off catch-up for this task (see below). By default a
  task that was missed while the host was off runs once on the next boot.

The `--command` is arbitrary shell, run from the repo root (`/mngr/code`). Use
repo-relative paths (`scripts/...`, `runtime/...`) just as you would in
`supervisord.conf`.

## Remove a task

```bash
scheduler remove <name>
```

To pause a task without losing its definition, re-add it with `--disabled` (or
edit `enabled = false` in the file) rather than removing it outright.

## The `[[task]]` schema

Each task is one `[[task]]` table in `runtime/scheduled_tasks.toml`:

```toml
[[task]]
name = "backup-notes"                       # unique id for the task
schedule = "0 3 * * *"                       # standard 5-field cron expression
command = "bash scripts/backup_notes.sh"     # arbitrary shell, run from the repo root
enabled = true                               # false switches the task off without deleting it
catch_up = true                              # run once on boot if a run was missed during downtime
description = "Nightly backup of the user's notes."
```

- `name` -- unique identifier. The CLI subcommands (`show`, `remove`) take it.
- `schedule` -- a standard 5-field cron expression (see below).
- `command` -- the shell command to run, from the repo root.
- `enabled` -- set `false` to keep the definition but stop it running.
- `catch_up` -- see "Catch-up semantics" below.
- `description` -- a short, plain explanation of the task.

## Cron syntax

The `schedule` field is a standard 5-field cron expression:

```
┌───────────── minute        (0-59)
│ ┌───────────── hour        (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month   (1-12)
│ │ │ │ ┌───────────── day of week (0-6, Sunday = 0)
│ │ │ │ │
* * * * *
```

Common forms:

- `0 3 * * *` -- 3 AM every day.
- `0 * * * *` -- once an hour, on the hour.
- `*/15 * * * *` -- every 15 minutes.
- `30 9 * * 1` -- 9:30 AM every Monday.
- `0 0 1 * *` -- midnight on the first of each month.

Schedules run in the user's local timezone when the host knows it; otherwise the
scheduler falls back to the host clock. You do not configure the timezone here.

## Catch-up semantics

If the host was off when a task was due, the scheduler runs it **once** shortly
after the next boot (for tasks with `catch_up = true`, the default). Multiple
missed runs **coalesce into a single run** -- e.g. an hourly task that was missed
for three hours runs exactly once on boot, not three times. Set `catch_up = false`
(or pass `--no-catch-up`) for a task that should simply be skipped when missed
rather than backfilled.

## When changes take effect

The scheduler re-reads `runtime/scheduled_tasks.toml` on its next tick, so an
add, remove, enable, or disable applies within about a minute. There is no
service to restart.
