- Wired the new file-driven `scheduler` service into the runtime. It runs as a
  supervised background service (`[program:scheduler]` in `supervisord.conf`,
  `command=uv run scheduler run`, logs under
  `/var/log/supervisor/scheduler-{stdout,stderr}.log`), so scheduled tasks fire
  once a minute and catch up runs missed while the container was offline.

- Registered the `scheduler` library (`libs/scheduler/`) as a workspace member,
  dependency, and uv source in the root `pyproject.toml`, mirroring the other
  `libs/*` services.

- On first boot, the bootstrap now seeds a default `runtime/scheduled_tasks.toml`
  (only if absent, so a user who has re-timed, disabled, or removed the task is
  never overwritten) containing a single nightly **Caretaker** task that runs at
  3 AM. It also creates the `runtime/scheduler/` and `runtime/caretaker/`
  directories the scheduler and Caretaker write into. The schedule file is
  plain, commented TOML the user (or an agent) can edit to change the time or
  turn the task off; changes apply within a minute, no restart needed.
