---
name: manage-scheduled-tasks
description: Query and edit the recurring scheduled jobs that run on this host. Use when you (or the user, via you) want to see what is scheduled, add a new recurring job, change when something runs, or stop a job from running. Jobs are plain cron and anacron entries -- anacron for daily jobs that must catch up after downtime, cron for precise or sub-daily schedules. Also covers how the built-in daily Caretaker job is wired and where all the scheduling configuration lives.
---

# Managing scheduled tasks

Recurring jobs on this host run through the stock OS tools, **cron** and
**anacron**. There is no custom scheduler: what runs and when is exactly what
`/etc/anacrontab` and `/etc/cron.d/` say.

## First: choose anacron or cron

Pick per job, based on what matters more:

- **anacron** -- for jobs that should run **about once a day (or coarser)** and
  **must not be skipped** when the machine was off or asleep at the scheduled
  moment. Anacron tracks the last run date per job; whenever it is triggered it
  runs any job whose period has lapsed, so a missed day is made up at the next
  opportunity (and several missed days collapse into one run). The cost: you
  cannot pick a precise time of day -- a daily job runs at the first trigger of
  the new day (usually shortly after local midnight, or at boot).
- **cron** -- for jobs that need a **precise time or a sub-daily cadence**
  (every 15 minutes, 9:30 on Mondays, ...). Cron fires exactly on schedule --
  but **only if the machine is up at that moment**; a missed run is simply
  skipped, never made up.

If the user asks for "daily-ish and reliable", use anacron. If they ask for "at
exactly HH:MM" or "every N minutes/hours", use cron -- and if the job also must
not be missed, say so: with cron it will be skipped when the machine is off.

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

A fixed-offset `Etc/GMT*` value in `/etc/timezone` means the boot-time fetch
failed and the bootstrap picked a placeholder (chosen so the Caretaker's first
run landed about 8 hours after setup) -- replace it with the user's real zone.
If they differ, update the container before writing the schedule entry:

```bash
ln -sf "/usr/share/zoneinfo/<Area/City>" /etc/localtime
echo "<Area/City>" > /etc/timezone
supervisorctl restart cron    # cron caches the timezone at daemon start
```

Anacron reads the clock per invocation, so it needs no restart.

## Every job needs the env wrapper

Cron and anacron give jobs a scrubbed, minimal environment -- none of the agent
environment (PATH with `uv`, `MNGR_*`, `LATCHKEY_*`, `GH_TOKEN`, ...) survives.
Prefix every job command with the wrapper, which restores the workspace
environment and runs the command from the repo root:

```
/mngr/code/scripts/with_agent_env.sh <command...>
```

Also redirect output to a log file (cron would otherwise try to mail it):
`>> /var/log/supervisor/<job-name>.log 2>&1`.

## Add an anacron job (daily+, catches up)

Append a line to `/etc/anacrontab`. Format: four fields --
`<period-days> <delay-minutes> <unique-job-id> <command>`:

```
1   5   backup-notes   /mngr/code/scripts/with_agent_env.sh bash scripts/backup_notes.sh >> /var/log/supervisor/backup-notes.log 2>&1
```

- `period-days` -- `1` = daily, `7` = weekly (or `@monthly`).
- `delay-minutes` -- wait this many minutes after the trigger before starting
  (staggers jobs; pick a small unique value).
- `job-id` -- unique name; anacron tracks the last run date under
  `/var/spool/anacron/<job-id>`.

Anacron re-reads `/etc/anacrontab` on every invocation (triggered every
minute between 03:00 and 23:59), so a new entry takes effect with nothing to
reload; a new daily job first fires at the next 3 AM.

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
2. **Schedule the shared runner** with the skill name as its argument -- as an
   anacron entry (daily, catch-up) or a cron entry (precise time), per the
   choice above. For example, daily via anacron:

   ```
   1   10   news   /mngr/code/scripts/with_agent_env.sh bash scripts/run_task_agent.sh news >> /var/log/supervisor/news-job.log 2>&1
   ```

That is all -- no new agent template is required. `scripts/run_task_agent.sh
<skill>` creates a persistent singleton agent (labelled `task_agent=<skill>`),
keeps it alive across runs, and on each run clears its chat and re-sends
`/<skill>`, so the skill runs fresh. The agent surfaces as a tab in the minds UI
and re-flashes on each run. Pass `--template <t>` only when you want a custom
agent template; otherwise the generic `task_agent` template is used.

## How the Caretaker is wired (the built-in example)

The nightly Caretaker is exactly the task-agent pattern above, with a tailored
agent template. Its entry is the `caretaker` line in `/etc/anacrontab` (period
1 day, no start delay, job id `caretaker`):

```
1   0   caretaker   /mngr/code/scripts/with_agent_env.sh bash /mngr/code/scripts/run_task_agent.sh caretaker --template caretaker >> /var/log/supervisor/caretaker-job.log 2>&1
```

- **What it runs:** the env wrapper around `bash scripts/run_task_agent.sh
  caretaker --template caretaker` (wake the singleton Caretaker agent for one
  run), with output appended to `/var/log/supervisor/caretaker-job.log`.
- **How it got there:** appended to `/etc/anacrontab` at image build by
  `scripts/build_workspace.sh`, grep-guarded on the job id so re-runs of that
  script never duplicate it. The script does not run again during the
  container's life, so deleting the line is how the Caretaker is switched off
  -- and it stays off.
- **When it fires:** once a day at 3 AM local time (never at workspace
  creation). Anacron is not a daemon; each invocation runs whatever is due and
  exits. There is exactly one trigger: `/etc/cron.d/fct-anacron` invokes it
  every minute between 03:00 and 23:59 (written by `scripts/setup_system.sh`,
  which also installs the cron and anacron packages and removes Debian's stock
  anacron trigger), so a daily job first becomes eligible at 3 AM each day.
  The first in-window tick after boot doubles as catch-up -- a day missed
  while the container was off runs within a minute of cron starting.
- **The first run:** at first boot the bootstrap seeds the caretaker's spool
  stamp (`/var/spool/anacron/caretaker`) with today's date, so the Caretaker
  never spawns on creation day; its first run is the next day's 3 AM. When the
  user's timezone cannot be fetched at first boot, the bootstrap instead
  adopts a fixed-offset `Etc/GMT*` zone that places the workspace's local
  clock at 19:00 at setup, so the first 3 AM window lands about 8 hours after
  setup; the real timezone replaces it once known.

## See, pause, or remove a job

- **List:** `cat /etc/anacrontab` and `ls /etc/cron.d/` (read the files -- they
  are the complete truth about what is scheduled).
- **Remove:** delete the anacrontab line or the `/etc/cron.d/<job-name>` file.
- **Pause without losing the definition:** comment the line out with `#`.

## Where the configuration lives

The complete map of the scheduling machinery, for edits and debugging:

- `/etc/anacrontab` -- daily-and-coarser jobs with missed-run catch-up
  (anacron re-reads it per invocation); the Caretaker's entry lives here.
- `/etc/cron.d/` -- one drop-in file per precise or sub-daily job (cron
  rescans the directory within a minute).
- `/etc/cron.d/fct-anacron` -- the every-minute anacron trigger.
- `supervisord.conf` -- `[program:cron]` is the cron daemon (check it with
  `supervisorctl status cron`).
- `/var/spool/anacron/<job-id>` -- anacron's per-job last-run stamps (how it
  knows a period has lapsed).
- `/var/log/supervisor/<job>.log` -- each job's own output (per the redirect
  on its entry); `/var/log/supervisor/cron-*.log` -- the cron daemon's logs.
- `/run/fct-agent-env` -- the per-boot agent-environment snapshot that
  `scripts/with_agent_env.sh` sources.
- `/etc/localtime` + `/etc/timezone` -- the container clock, set from the
  user's timezone at each boot by the bootstrap (see the timezone section
  above for re-checking it).
