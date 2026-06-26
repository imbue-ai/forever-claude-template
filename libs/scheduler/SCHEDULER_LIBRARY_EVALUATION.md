# Scheduler library evaluation

**Question:** Is there a well-established scheduling library that does what our
custom `scheduler` does, so we don't have to maintain our own? The user framed
the need as "something for Linux that's like cron, but does missed tasks."

**Bottom line (TL;DR):** No library cleanly covers the combination we need
(offline catch-up with on-disk state + supervisord-hosted Python service +
file-driven config + shell-command tasks). The closest fit, **APScheduler 3.x**,
*can* be configured to replay missed runs, but only by overriding its defaults,
and it would replace only ~150 of our ~440 source lines while *adding* a
TOML-to-jobstore reconciliation layer. **Recommendation: keep the thin custom
scheduler.** Details, evidence, and a contingency migration plan below.

---

## 1. What ours does

`libs/scheduler/` is a small, file-driven task scheduler for the agent
workspace. It runs as a supervised background service and executes recurring
shell commands on a cron schedule, with one property plain cron lacks: **offline
catch-up**.

- **Config (file-driven).** The schedule is one human- and agent-editable TOML
  file, `runtime/scheduled_tasks.toml`. Each `[[task]]` has `name`, a standard
  5-field cron `schedule`, a shell `command`, `enabled`, `catch_up`, and
  `description`. Agents edit it through validated CLI subcommands
  (`scheduler add/remove/list/show`) -- `src/scheduler/schedule_file.py`,
  `src/scheduler/cli.py`.
- **How it runs.** The daemon is `uv run scheduler run`
  (`src/scheduler/cli.py` -> `run_loop` in `src/scheduler/runner.py`), launched
  as a long-lived supervisord program (`supervisord.conf` `[program:scheduler]`,
  `autostart=true`, `autorestart=true`). It ticks once a minute, reads the TOML,
  and launches each due task as a background `subprocess.Popen` (shell command,
  output appended to `runtime/scheduler/logs/<name>.log`). A running-guard
  prevents relaunching a task that is still running from a prior tick. It runs
  an immediate tick on startup so downtime is caught up without waiting a full
  interval.
- **Missed-task catch-up (the defining feature).** Per-task last-run state lives
  in `runtime/scheduler/state.toml` (`src/scheduler/state.py`), written
  atomically (temp file + `os.replace`) so it survives restarts. The pure timing
  core (`src/scheduler/engine.py`) decides due-ness with no I/O and no clock of
  its own:
  - A task is **due** when its *most recent* scheduled fire time (computed with
    `croniter.get_prev`) is strictly after its recorded `last_run_at`. Because
    only the single most recent fire is considered, **several intervals missed
    during downtime coalesce into one run**.
  - `catch_up = false` tasks only run if that fire is within the current tick
    window; a stale miss is skipped.
  - **Arming:** a newly seen task records `last_run_at = now` *without running*,
    so adding/seeding a task never triggers an immediate fire -- it first runs at
    its next scheduled time.
- **Timezone.** Schedules are interpreted in an IANA tz read from
  `runtime/scheduler/timezone`, falling back to the host clock
  (`src/scheduler/config.py`).

Source size (the part a library could plausibly replace is small):

| File | Lines | Replaceable by a scheduling library? |
|---|---:|---|
| `engine.py` (timing/catch-up core) | 95 | **Yes** -- this is the cron + misfire logic |
| `state.py` (on-disk last-run state) | 57 | Partly -- a persistent jobstore subsumes it |
| `schedule_file.py` (TOML config I/O) | 116 | No -- our file-driven model has no library equivalent |
| `runner.py` (shell exec + per-task logs + running-guard) | 137 | No -- libraries run Python callables, not logged shell subprocesses |
| `cli.py` (`run`/`add`/`remove`/`list`/`show`) | 125 | No |
| `config.py` (tz resolution) | 41 | No |
| `data_types.py`, `errors.py` | 64 | No |
| **Total source** | **~440** | ~150 lines overlap a library's job |

## 2. Requirements (a candidate must satisfy ALL five)

1. **Cron-style schedules** (e.g. `0 3 * * *`).
2. **File/config-driven task list** (TOML is the source of truth, edited by
   agents/users).
3. **Long-lived Python service under supervisord** -- *not* systemd, *not* the
   system cron daemon, both of which are unavailable in the workspace container.
4. **Offline catch-up of missed runs after downtime, with last-run state
   persisted to disk so it survives container restarts.** This is the
   make-or-break feature.
5. **Python** (the service is `uv run scheduler run`).

## 3. Candidate comparison

| Candidate | 1. Cron | 2. File-driven | 3. supervisord/Python svc | 4. Offline catch-up w/ persisted state | 5. Python | Verdict |
|---|---|---|---|---|---|---|
| **Our custom scheduler** | Yes (`croniter`) | Yes (TOML) | Yes | **Yes, by design** (state.toml + coalesced replay + arming) | Yes | baseline |
| **APScheduler 3.x** | Yes (`CronTrigger`) | No (jobstore is source of truth; jobs added via `add_job`) | Yes (`BackgroundScheduler` in a Python process) | **Only with non-default config:** persistent jobstore + `misfire_grace_time=None` + `coalesce=True` | Yes | Closest, but not clean (see below) |
| **APScheduler 4.x** | Yes | No | Yes | Conceptually yes | Yes | **Disqualified: alpha, "not for production"** |
| **`schedule`** | No (no cron syntax) | No | Yes | **No** -- "no built-in persistent storage; jobs/missed runs lost on restart" | Yes | Disqualified |
| **`croniter`** | Yes (parser) | n/a | n/a (it's a library, not a scheduler) | No (parsing only -- we already use it) | Yes | Not a scheduler |
| **Celery beat** | Yes | Partly (config schedule) | Heavyweight (needs a broker) | **No catch-up** -- `PersistentScheduler` only tracks last-run to avoid *double*-firing; it does not replay intervals missed during downtime | Yes | Disqualified (no catch-up; broker overkill) |
| **systemd timers** (`Persistent=true`) | calendar syntax | unit files | **No -- requires systemd as PID 1** | Yes (anacron-like, `stamp-*` files) | No | **Disqualified: workspace uses supervisord, not systemd** |
| **anacron** | day-granularity only | `/etc/anacrontab` | **No -- OS daemon, not Python** | Yes | No | **Disqualified: needs cron/systemd; not sub-day; not Python** |

### Per-candidate evidence

**APScheduler 3.x -- the only serious library contender.**
Its persistent jobstore + misfire mechanism *can* replay runs missed while the
process was down, which is exactly our requirement -- but **not with default
settings**, and the defaults are a footgun:

- Verified in the APScheduler 3.x source (`apscheduler/schedulers/base.py`),
  the global job defaults are `misfire_grace_time = 1` (one second) and
  `coalesce = True`. With the default 1-second grace, **any run missed by more
  than one second is skipped** -- i.e. the default behavior is the *opposite* of
  offline catch-up.
- The grace check lives in the executor (`apscheduler/executors/base.py`,
  `run_job`): `if job.misfire_grace_time is not None: if (now - run_time) >
  grace_time: skip`. So to "run no matter how late," you must set
  `misfire_grace_time=None`, which the docs describe as "allow the job to run no
  matter how late it is."
- Coalescing matches our "collapse missed intervals into one" exactly: in
  `base.py`, `run_times = run_times[-1:] if run_times and job.coalesce else
  run_times`.
- Persistence: jobs (and their `next_run_time`) survive restarts only with a
  persistent jobstore (e.g. `SQLAlchemyJobStore` on SQLite). The default
  `MemoryJobStore` loses everything on restart -- no catch-up.

So APScheduler 3.x covers requirements 1, 3, 4 (with `misfire_grace_time=None`,
`coalesce=True`, and a SQLite jobstore), and 5. It **fails requirement 2**: its
source of truth is the jobstore database and jobs are registered programmatically
via `add_job`, not a human/agent-editable file. Keeping our TOML as the source
of truth would require us to write a reconciliation layer that diffs the TOML
against the jobstore every tick and adds/modifies/removes jobs accordingly --
new code we don't have today.

**APScheduler 4.x.** Splits the job concept into Task/Schedule/Job and redesigns
data stores, but as of 4.0.0a6 (April 2025) it is **alpha**, explicitly "should
NOT be used in production," "may change in a backwards incompatible fashion
without any migration pathway," and has no 3.x->4.x schedule import. Disqualified
on maintenance/reliability grounds.

**`schedule` (dbader/schedule).** Simple and popular, but has **no cron syntax**
(only fluent `every().day.at(...)`) and **no persistence** -- "no built-in
mechanism for running jobs after a restart or for remembering missed runs; job
schedules do not persist across process restarts." Fails requirements 1, 2, 4.

**`croniter`.** A cron-expression parser, not a scheduler -- it has no run loop,
no state, no execution. We already depend on it (it *is* our timing primitive).
Not a candidate to replace the scheduler.

**Celery beat.** Designed for periodic task dispatch to workers via a broker
(Redis/RabbitMQ). The default `PersistentScheduler` keeps last-run times in a
`shelve` file only to avoid firing a task twice; it does **not** replay intervals
missed while beat was down. It also drags in a broker + worker fleet --
disproportionate for "run a shell command once a night." Fails requirement 4 and
is operationally far too heavy.

**systemd timers (`Persistent=true`) and anacron.** These are the OS-native
"cron that catches up after downtime," and `Persistent=true` does exactly what we
want (writes `stamp-*` files, replays overdue timers on boot). **But they are
disqualified by requirement 3:** the workspace container is supervised by
**supervisord**, not systemd (no PID 1 systemd, no `systemctl`), and the system
cron/anacron daemons are not part of the service model. They are also not Python
(requirement 5) and anacron's granularity is per-day, too coarse for arbitrary
cron schedules. Calling these out explicitly per the brief: **they would be the
obvious answer on a normal Linux host, but they don't fit this container's
supervisord-based service model.**

## 4. Recommendation: keep the thin custom scheduler

No single well-established library satisfies all five requirements without us
writing additional glue. The make-or-break feature -- replaying runs missed
while the *process was down*, from on-disk state -- is matched only by
APScheduler 3.x (with non-default config) and by OS schedulers that the
supervisord model rules out.

Adopting APScheduler 3.x would be a **net complexity increase, not a reduction**:

- It would replace only our ~150-line timing/state core (`engine.py` + part of
  `state.py`), while **adding** a TOML<->jobstore reconciliation layer to keep
  the file-driven model (requirement 2).
- The parts that dominate our line count -- the file-driven TOML config and
  validated CLI (`schedule_file.py`, `cli.py`), and the shell-subprocess
  execution with per-task log files and the running-guard (`runner.py`) -- have
  **no library equivalent** and would remain regardless. APScheduler runs Python
  callables in a thread pool, not logged shell subprocesses, so `runner.py`'s job
  would still be ours.
- It would trade a **fully deterministic, I/O-free, trivially unit-testable**
  timing core (`engine.py` takes `now` and `tz` as arguments) for a heavier
  dependency whose **default configuration silently does the wrong thing**
  (`misfire_grace_time=1` skips exactly the missed runs we care about). That is a
  reliability regression risk for the one feature that matters most.
- Our scheduler currently has exactly **two non-trivial runtime dependencies**
  (`croniter` for parsing, `tomlkit` for config). It is already "using a library
  for the hard part" -- cron math is delegated to `croniter`. The remaining code
  is the irreducible glue (file config, shell exec, logging, supervisord
  entrypoint) that any solution needs.

The custom scheduler is small (~440 source lines), already leverages `croniter`
for the genuinely hard cron arithmetic, has a clean and fully unit-tested timing
core, and exactly matches the workspace's supervisord + file-driven model. The
right call is to **keep it**.

## 5. Contingency: migration plan, IF we ever adopt APScheduler 3.x

Recorded for completeness; **not recommended now**. If a future need (e.g.
multi-process scheduling, sub-minute precision, missed-run audit history)
justifies it, migrate to APScheduler 3.x while preserving the external interface:

- **Keep unchanged:** `runtime/scheduled_tasks.toml` as the source of truth; the
  `scheduler run` entrypoint and all `add/remove/list/show` CLI subcommands; the
  `[program:scheduler]` supervisord program; per-task logs under
  `runtime/scheduler/logs/`; the catch-up + arming semantics.
- **Replace `engine.py` + `state.py`** with a `BackgroundScheduler` configured
  with:
  - a persistent `SQLAlchemyJobStore` on
    `runtime/scheduler/jobstore.sqlite` (replaces `state.toml`; survives
    restarts),
  - global `job_defaults={'misfire_grace_time': None, 'coalesce': True}` (this is
    mandatory -- the defaults would break catch-up),
  - one `CronTrigger.from_crontab(task.schedule, timezone=...)` per task.
- **Add a reconciliation step** (new code): on startup and on each TOML change,
  diff `scheduled_tasks.toml` against the jobstore and add/modify/remove jobs to
  match. Preserve arming by setting a new job's `next_run_time` to its next fire
  (never the past).
- **Keep `runner.py`'s execution model** as the job function: a single callable
  that shells out (`subprocess`), appends to the per-task log, and enforces the
  running-guard (APScheduler's `max_instances=1` covers the latter natively).
- **Tests:** the existing `engine_test.py` / `state_test.py` would be rewritten
  against the reconciliation + trigger layer; `schedule_file_test.py` is
  unaffected. Re-verify offline catch-up explicitly: seed a past `next_run_time`,
  start the scheduler, assert the task fires exactly once.

The decisive caveat remains: this *adds* the reconciliation layer and depends on
non-default APScheduler settings for correctness, so it only makes sense if a
requirement appears that our current scheduler genuinely cannot meet.

## Sources

- APScheduler 3.x user guide (misfired jobs, coalescing):
  <https://apscheduler.readthedocs.io/en/3.x/userguide.html>
- APScheduler 3.x source -- default `misfire_grace_time=1`, `coalesce=True`
  (`apscheduler/schedulers/base.py`) and the per-run-time grace check
  (`apscheduler/executors/base.py`, `run_job`):
  <https://github.com/agronholm/apscheduler/tree/3.10.4>
- APScheduler 4.x is alpha / not for production
  (4.0.0a6, April 2025): <https://pypi.org/project/APScheduler/> and
  <https://apscheduler.readthedocs.io/en/master/migration.html>
- `schedule` has no persistence / no cron / no catch-up:
  <https://github.com/dbader/schedule>
- Celery beat `PersistentScheduler` tracks last-run times (no downtime replay):
  <https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html>
- systemd timers `Persistent=true` (anacron-like catch-up, needs systemd):
  <https://wiki.archlinux.org/title/Systemd/Timers>
- anacron overview: <https://oneuptime.com/blog/post/2026-03-02-how-to-use-anacron-for-scheduling-on-systems-that-are-not-always-on/view>
