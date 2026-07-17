---
name: manage-scheduled-tasks
description: Query and edit the recurring scheduled jobs that run on this host. Use when you (or the user, via you) want to see what is scheduled, add a new recurring job, change when something runs, or stop a job from running. Jobs are plain cron entries in /etc/cron.d/ -- daily jobs that must catch up after downtime tick every minute through the run_daily_job.sh due-checker, precise or sub-daily schedules are ordinary cron lines. Also covers how the built-in weekly Caretaker job is wired (off by default) and where all the scheduling configuration lives.
---

# Managing scheduled tasks

Recurring jobs on this host run through the stock **cron** daemon: what runs
and when is exactly what the drop-in files in `/etc/cron.d/` say. Cron has
one failure mode that drives every choice below: **it only fires when the
machine is up at that moment** -- a job whose time passes while the container
is off or asleep is simply skipped, never made up. When a job must not be
missed, run it through a small script, `scripts/run_daily_job.sh` (~50
lines): an every-minute cron line ticks it, and it runs the job at its due
hour when the machine is up -- or the first minute the machine is back up
after a missed day. The built-in weekly **Caretaker** is the worked example
of that pattern (see below).

## First: choose the daily-job pattern or a plain cron line

Pick per job, based on what matters more:

- **daily job with catch-up** (`run_daily_job.sh`) -- for jobs that should run
  **about once a day (or coarser)** at a due hour and **must not be skipped**
  when the container was off or asleep at that moment. A cron line ticks every
  minute and hands the decision to the checker, which runs the job at most
  once per calendar day: at the due hour when the container is up, or the
  first minute the container is back up after a missed day -- at any hour.
- **plain cron line** -- for jobs that need a **precise time or a sub-daily
  cadence** (every 15 minutes, 9:30 on Mondays, ...). Cron fires exactly on
  schedule, with the caveat above: a missed run is skipped, never made up.

If the user asks for "daily-ish and reliable", use the daily-job pattern. If
they ask for "at exactly HH:MM" or "every N minutes/hours", use a plain cron
line -- and if the job also must not be missed, say so: with a plain cron line
it will be skipped when the machine is off.

## Timezone: confirm it before scheduling anything

The container's clock is set to the **user's local timezone at each boot** (the
bootstrap fetches it from the minds app on the user's machine). But the user may
have moved since boot, so **when the user asks to schedule something, re-check
their current timezone first**:

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/timezone
# -> {"timezone": "America/Los_Angeles"}   ("" means unknown -- keep the current setting)
cat /etc/timezone                          # what the container currently uses
```

If the boot-time fetch failed, the container is still on UTC -- replace it
with the user's real zone. If they differ, update the container before writing
the schedule entry:

```bash
ln -sf "/usr/share/zoneinfo/<Area/City>" /etc/localtime
echo "<Area/City>" > /etc/timezone
```

Daily jobs pick the change up immediately -- `run_daily_job.sh` reads the
clock on every tick. Precise cron schedule lines additionally need
`supervisorctl restart cron`: the cron daemon caches the timezone it uses to
match those.

## Every job needs the env wrapper

Cron gives jobs a scrubbed, minimal environment -- none of the agent
environment (PATH with `uv`, `MNGR_*`, `LATCHKEY_*`, `GH_TOKEN`, ...) survives.
Prefix every job command with the wrapper, which restores the workspace
environment and runs the command from the repo root:

```
/mngr/code/scripts/with_agent_env.sh <command...>
```

Also redirect output to a log file (cron would otherwise try to mail it):
`>> /var/log/supervisor/<job-name>.log 2>&1`.

## Add a daily job (with catch-up)

Drop a file in `/etc/cron.d/<job-name>` (mode 0644) with a line that ticks
every minute through the wrapper and the checker:

```
* * * * *   root   /mngr/code/scripts/with_agent_env.sh /mngr/code/scripts/run_daily_job.sh <job-id> <due-hour> <command...> >> /var/log/supervisor/<job>.log 2>&1
```

- `job-id` -- unique name; the checker records the last covered date under
  `/var/lib/minds/daily-stamps/<job-id>`.
- `due-hour` -- the local hour (0-23) the job is due.
- `--interval-days N` (optional, before the command) -- run every N days
  instead of daily, same due-hour and catch-up rules over the N-day window
  (the Caretaker uses 7).

The every-minute tick is what makes catch-up possible: the checker exits
instantly on every tick where nothing is due, and a flock held for the job's
whole duration makes overlapping ticks skip. The semantics:

- **At most one run per calendar day** -- the stamp marks the day covered.
- **Runs at the due hour** when the container is up at that moment.
- **Catch-up at any hour:** if a whole day was missed while the container was
  off, the job runs within the first minute the container is up again --
  whatever the hour.
- **Silent after a covered day:** the night after a successful run, nothing
  fires at midnight (nothing was missed).
- **Failures retry daily, not every minute:** the stamp is written before the
  job starts, so a failing run is retried the next day; look for failures in
  the job's log.

A job with no stamp yet runs at the first tick at or after its due hour -- so
a job added in the afternoon with a morning due hour fires within a minute. To
make it wait for tomorrow instead, seed the stamp with today's date first:
`mkdir -p /var/lib/minds/daily-stamps && date +%F > /var/lib/minds/daily-stamps/<job-id>`
Cron rescans
`/etc/cron.d/` within a minute, so a new drop-in takes effect with nothing to
reload.

## Add a cron job (precise schedule, no catch-up)

Drop a file in `/etc/cron.d/<job-name>` (mode 0644). Format: standard 5-field
cron schedule, then the **user** (always `root` here), then the command:

```
30 9 * * 1   root   /mngr/code/scripts/with_agent_env.sh bash scripts/weekly_report.sh >> /var/log/supervisor/weekly-report.log 2>&1
```

The 5 schedule fields are minute (0-59), hour (0-23), day of month (1-31),
month (1-12), day of week (0-6, Sunday = 0). Common forms: `0 3 * * *` = 3 AM
daily; `*/15 * * * *` = every 15 minutes; `0 0 1 * *` = midnight on the 1st.
One quirk: `%` is special in cron commands (means newline) -- escape it as `\%`
(e.g. `date +\%F`). Cron rescans `/etc/cron.d/` within a minute; no reload.

## Set up an agent task (run a skill on a schedule)

A **task agent** is a scheduled job that, instead of running a plain script,
wakes a dedicated agent to run one skill in its own chat tab. To add one -- say
a morning news digest:

1. **Write the skill** at `.agents/skills/<name>/SKILL.md` -- the instructions
   the agent follows on each run (see the existing skills for the shape).
2. **Schedule the shared runner** with the skill name as its argument --
   through the daily-job checker (daily, catch-up) or as a plain cron line
   (precise time), per the choice above. For example, daily at 9 AM with
   catch-up, in `/etc/cron.d/news`:

   ```
   * * * * *   root   /mngr/code/scripts/with_agent_env.sh /mngr/code/scripts/run_daily_job.sh news 9 bash /mngr/code/scripts/run_task_agent.sh news >> /var/log/supervisor/news-job.log 2>&1
   ```

That is all -- no new agent template is required. `scripts/run_task_agent.sh
<skill>` creates a persistent singleton agent (labelled `task_agent=<skill>`),
keeps it alive across runs, and on each run clears its chat and re-sends
`/<skill>`, so the skill runs fresh. The agent surfaces as a tab in the minds UI
and re-flashes on each run. Pass `--template <t>` only when you want a custom
agent template; otherwise the generic `task_agent` template is used.

## How the Caretaker is wired (the built-in example)

The Caretaker is the task-agent pattern above with two extra gates in front:
it is **off by default**, and even when on, the agent only wakes when a
deterministic check found something. Its entry is the single line in
`/etc/cron.d/minds-caretaker`:

```
* * * * *   root   /mngr/code/scripts/with_agent_env.sh bash /mngr/code/scripts/caretaker_check.sh >> /var/log/supervisor/caretaker-job.log 2>&1
```

- **The gate script** (`scripts/caretaker_check.sh`) exits immediately unless
  `runtime/caretaker/enabled` exists (created by the enable-caretaker skill).
  When enabled, it hands timing to `run_daily_job.sh` (job id `caretaker`,
  due hour 3, `--interval-days 7`), which re-invokes it with `--fire` when a
  check is due.
- **The deterministic check** (`--fire`) looks for services in FATAL/BACKOFF,
  fresh error output in `/var/log/supervisor/` since the last check, disk at
  or above 85 percent, and new OOM-guard shedding. Findings are written to
  `runtime/caretaker/findings.md` and the Caretaker agent is woken via
  `run_task_agent.sh caretaker --template caretaker`; with no findings,
  nothing runs until the next weekly check. The one exception: if the agent
  has never introduced itself (no `runtime/caretaker/permissions.md`), it is
  woken once regardless of findings.
- **How it got there:** written to `/etc/cron.d/minds-caretaker` at image build
  by `scripts/build_workspace.sh`, guarded on the file's existence so re-runs
  of that script never recreate it. `rm runtime/caretaker/enabled` (the
  disable-caretaker skill) switches the Caretaker off; deleting the cron file
  removes even the no-op tick.
- **When the agent runs:** at most once a week, at 3 AM local when the
  container is up (first minute back up after an overdue window otherwise),
  and only with findings -- plus the one-time introduction shortly after the
  user enables it.

## See, pause, or remove a job

- **List:** `ls /etc/cron.d/` and read the files -- they are the complete
  truth about what is scheduled.
- **Remove:** delete the `/etc/cron.d/<job-name>` file.
- **Pause without losing the definition:** comment the line out with `#`.
- **Check a daily job's state:** read `/var/lib/minds/daily-stamps/<job-id>` --
  the last date the job covered. Deleting the stamp makes today eligible
  again (the job runs at the next tick at or after its due hour).

## Where the configuration lives

The complete map of the scheduling machinery, for edits and debugging:

- `/etc/cron.d/` -- one drop-in file per job, both kinds: daily jobs are
  every-minute lines through `run_daily_job.sh`, precise jobs are ordinary
  schedule lines (cron rescans the directory within a minute).
- `/etc/cron.d/minds-caretaker` -- the Caretaker's drop-in.
- `/mngr/code/scripts/run_daily_job.sh` -- the daily-job due-checker (all of
  the daily-scheduling logic, about 50 lines).
- `/var/lib/minds/daily-stamps/<job-id>` -- each daily job's last covered date
  (how the checker knows whether today has run and whether a day was missed).
- `supervisord.conf` -- `[program:cron]` is the cron daemon (check it with
  `supervisorctl status cron`).
- `/var/log/supervisor/<job>.log` -- each job's own output (per the redirect
  on its entry); `/var/log/supervisor/cron-*.log` -- the cron daemon's logs.
- `/run/minds-agent-env` -- the per-boot agent-environment snapshot that
  `scripts/with_agent_env.sh` sources.
- `/etc/localtime` + `/etc/timezone` -- the container clock, set from the
  user's timezone at each boot by the bootstrap (see the timezone section
  above for re-checking it).
