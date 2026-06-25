- Added a `manage-scheduled-tasks` skill that teaches agents how to query and
  edit the host's recurring schedule (`runtime/scheduled_tasks.toml`): listing
  tasks with `scheduler list`, adding/removing them with the `scheduler` CLI, the
  `[[task]]` schema and cron syntax (with examples), catch-up semantics for runs
  missed during downtime, and that edits take effect within about a minute with no
  restart. It steers agents toward the CLI over hand-editing the file while the
  scheduler service may be writing it.

- Added a `check-app-errors` skill that teaches agents to survey app health: run
  `supervisorctl status`, scan `/var/log/supervisor/*-stderr.log` (and stdout) for
  errors and tracebacks with efficient search commands, and summarize what is
  wrong and where. It is reusable by both day-to-day chat agents and the
  Caretaker's nightly log scan.

- Updated `CLAUDE.md` to remind agents to proactively check
  `/var/log/supervisor/` for errors after building or editing any app or service,
  pointing them at the `check-app-errors` skill (a clean exit code does not mean
  the service is healthy).
