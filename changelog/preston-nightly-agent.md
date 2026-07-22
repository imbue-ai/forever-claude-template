Recurring jobs via cron, and the Caretaker. Adds recurring-job scheduling
to every workspace using cron plus a tiny due-checker, and a weekly
**Caretaker** agent built on top of it -- off by default (BETA), gated behind
a deterministic check that wakes the agent only when it finds something --
plus the supporting skills, docs, and in-workspace tab behavior that make
scheduled agents visible.

**Recurring jobs with cron + a daily due-checker.** Workspaces schedule
recurring work with **cron** (`/etc/cron.d/` drop-ins, the daemon running
under supervisord as `[program:cron]`): plain cron lines for precise times or
sub-daily cadences, exact but never backfilled, and a daily-job pattern for
jobs that must not be skipped when the container was off -- an every-minute
cron line hands the decision to `scripts/run_daily_job.sh`, a ~50-line
due-checker that stamps the last covered date per job and runs each job at
most once per interval (daily by default; `--interval-days N` for coarser
cadences like the Caretaker's weekly): at its due hour when the container is
up, or within the first minute the container is back up after the window was
fully missed, at any hour -- and after a covered window, nothing fires at
midnight. The stamp is written before the job starts, so a failed run retries
on the next due day rather than every minute. Plain cron alone cannot provide
this: it fires only when the machine is up at that moment and never backfills
a missed run -- the checker exists precisely to add "due at the due hour, but
catch up a missed window the first minute the container is back, at any
hour". Because cron scrubs
the job environment, a small wrapper (`scripts/with_agent_env.sh`) rebuilds
the workspace environment from the env files mngr maintains on the host dir
(the same way mngr sources them for agent operations), and every scheduled
job runs through it. The container's clock is set to the
user's local timezone at each boot: the bootstrap pulls it from the minds
desktop client's `GET /api/v1/timezone` through the latchkey gateway (falling
back to UTC when unreachable), so schedules run in the user's local time. (An
earlier iteration of this branch built a custom `libs/scheduler` service --
about 635 lines -- for the catch-up behavior; the checker replaced it with
about 50 lines of shell.)

**Scheduled agent tasks and the Caretaker.** A scheduled job can wake an agent
that runs a skill on a cadence, in its own chat tab. `scripts/run_schedule_agent.sh
<skill>` spawns a single persistent agent for that skill; on each run mngr clears
the agent's session and re-sends `/<skill>` so the skill runs fresh, with no memory
of the previous run.
A new scheduled agent (e.g. a morning news digest) needs only a skill plus a
cron entry -- no new agent template. The weekly **Caretaker** is the built-in
instance -- and it is **off by default, as a BETA feature**: no cron entry
exists until the user turns it on. The new **enable-caretaker** skill (used
only when the user explicitly asks) explains the beta status, gets an
explicit yes, and enables it by writing `/etc/cron.d/minds-caretaker` -- an
ordinary daily-job entry whose weekly due-checker execs
`scripts/caretaker_check.sh` when a check is due. Once enabled, the agent
introduces itself shortly afterwards, and from then on each due check runs a
deterministic scan -- services in FATAL/BACKOFF, fresh error output in the
service logs since the last check, disk at or above 85 percent, new OOM-guard
shedding -- and wakes the agent **only when it found something**, telling it
what's up via `runtime/caretaker/findings.md`. When woken, the Caretaker
verifies the findings, checks basic system health and finished-but-uncommitted
work (committing it, with permission, so it is safely in history), and either
fixes what it found or explains it, always in plain, non-technical language.
On its very first run it does one look-only scan (changing nothing), then
introduces itself and asks whether to keep checking each week and whether to
fix small things on its own. Each run starts from a fresh session (no memory
of the prior run); it remembers your choices and what it saw before through
its own notes on disk, not the conversation. Your standing permissions live in
a single plain-language `runtime/caretaker/permissions.md` that the Caretaker
reads each run and rewrites when you change your mind, and that you can edit
yourself any time. You stay in full control: the
equally short **disable-caretaker** skill (`rm /etc/cron.d/minds-caretaker`)
switches it off entirely -- while off, nothing runs at all.

**Health-check skills and docs.** Adds a `check-app-errors` skill (survey
`supervisorctl status`, scan `/var/log/supervisor/` for errors and tracebacks,
summarize what's wrong and where), reusable by both day-to-day chat agents and the
Caretaker's weekly scan; and a `manage-scheduled-tasks` skill that teaches agents
to choose between the daily catch-up pattern and plain cron lines per job, the
entry formats, the env wrapper, and to
re-check the user's current timezone (via the minds timezone endpoint) before
scheduling anything, updating the container clock if the user has moved.
The full scheduling detail (daily catch-up vs. plain cron, entry formats, the
env wrapper, the timezone check, the Caretaker wiring) lives in the
manage-scheduled-tasks skill; CLAUDE.md gains just one sentence pointing at the
manage-scheduled-tasks and check-app-errors skills.

**Surfacing scheduled agents in the workspace** needs no UI changes at all:
at the start of each run the woken agent surfaces its own chat tab (focused)
with one best-effort `scripts/layout.py open` call -- the same existing
mechanism web apps are surfaced with. Doing it from inside the agent avoids
the create-time race where the browser has not yet learned a brand-new agent.

