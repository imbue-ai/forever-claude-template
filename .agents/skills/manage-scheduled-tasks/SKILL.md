---
name: manage-scheduled-tasks
description: Query and edit the recurring scheduled jobs that run on this host. Use when you (or the user, via you) want to see what is scheduled, add a new recurring job, change when something runs, or stop a job from running. Jobs are plain cron and anacron entries -- anacron for daily jobs that must catch up after downtime, cron for precise or sub-daily schedules.
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
environment (from the snapshot the bootstrap writes each boot) and runs the
command from the repo root:

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

Anacron re-reads `/etc/anacrontab` on every invocation -- it is triggered once
per boot and hourly (via `/etc/cron.d/fct-anacron`), so a new entry takes effect
within the hour with nothing to reload.

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
wakes a dedicated agent to run one skill in its own chat tab. The nightly
Caretaker is the built-in example. To add your own -- say a morning news digest:

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
and re-flashes on each run.

The Caretaker is just this pattern with a tailored agent template: the
`caretaker` line in `/etc/anacrontab` runs
`scripts/run_task_agent.sh caretaker --template caretaker` once a day. Pass
`--template <t>` only when you want a custom agent template; otherwise the
generic `task_agent` template is used.

## See, pause, or remove a job

- **List:** `cat /etc/anacrontab` and `ls /etc/cron.d/` (read the files -- they
  are the complete truth about what is scheduled).
- **Remove:** delete the anacrontab line or the `/etc/cron.d/<job-name>` file.
  This is also how the user switches the Caretaker off: delete the `caretaker`
  line from `/etc/anacrontab`.
- **Pause without losing the definition:** comment the line out with `#`.
- **Run history:** each job's own log under `/var/log/supervisor/<job-name>.log`;
  anacron's last-run dates in `/var/spool/anacron/`.

## How the machinery runs (for debugging)

The cron daemon runs under supervisord (`[program:cron]` -- check
`supervisorctl status cron`, logs at `/var/log/supervisor/cron-*.log`). Anacron
is not a daemon: it is invoked once per boot by the `[program:anacron-boot]`
one-shot (this is what catches up jobs missed while the container was off) and
hourly by `/etc/cron.d/fct-anacron`; each invocation runs whatever is due and
exits.
