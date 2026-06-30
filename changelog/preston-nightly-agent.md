A nightly scheduler and the Caretaker. Adds a recurring-task scheduler to every
workspace, a nightly **Caretaker** agent built on top of it, and the supporting
skills, docs, and in-workspace tab behavior that make scheduled agents visible.

**A file-driven task scheduler.** A new `libs/scheduler` service runs recurring
shell commands on a cron schedule, but unlike plain cron it catches up on runs
missed while the machine was off: when the workspace comes back online, any task
whose time passed during the downtime runs once (multiple missed intervals
collapse into a single run). The schedule lives in one readable, commented file,
`runtime/scheduled_tasks.toml`, that users and agents can edit -- each task has a
name, a 5-field cron schedule, a shell command, and `enabled`/`catch_up` flags. A
`scheduler` CLI lists, adds, shows, and removes tasks; the service runs under
supervisord (`[program:scheduler]`) and applies edits within a minute with no
restart. On first boot the bootstrap seeds a default schedule (only if absent, so
a user's edits are never overwritten) with a single nightly Caretaker task at
3 AM, and creates the `runtime/scheduler/` and `runtime/caretaker/` directories.
(We evaluated off-the-shelf options -- APScheduler, systemd timers, anacron -- but
none cleanly fit a supervisord-based, file-as-source-of-truth, catch-up-from-disk
model, so the small custom scheduler is a net simplification.)

**Scheduled agent tasks and the Caretaker.** A scheduled task can wake an agent
that runs a skill on a cadence, in its own chat tab. `scripts/run_task_agent.sh
<skill>` spawns a single persistent agent for that skill; on each run mngr clears
its chat and re-sends `/<skill>` so the skill runs fresh in an empty conversation.
A new scheduled agent (e.g. a morning news digest) needs only a skill plus a
scheduler entry -- no new agent template. The nightly **Caretaker** is the
built-in instance: once a night it quietly checks the apps and services in your
workspace for problems -- a page that stopped loading, a service that crashed,
errors piling up -- and either fixes them or explains what it found, always in
plain, non-technical language. On its very first night it does one look-only scan
(changing nothing), then introduces itself with what it found and asks whether to
keep checking each night, fix small things on its own, or be switched off; from
the second night on it scans only once you've opted in. It greets you each night
before it starts, keeps the chat clean by starting each run fresh, and remembers
your choices and what it saw on previous nights through its own notes on disk, not
the conversation. Your standing permissions live in a single plain-language
`runtime/caretaker/permissions.md` that the Caretaker reads each run and rewrites
when you change your mind, and that you can edit yourself any time. You stay in
full control: change when it runs, give it other regular chores, or switch it off
entirely.

**Health-check skills and docs.** Adds a `check-app-errors` skill (survey
`supervisorctl status`, scan `/var/log/supervisor/` for errors and tracebacks,
summarize what's wrong and where), reusable by both day-to-day chat agents and the
Caretaker's nightly scan; and a `manage-scheduled-tasks` skill that teaches agents
the scheduler's CLI, the task schema and cron syntax, and catch-up semantics.
CLAUDE.md gains a "Scheduled tasks" section (when to use the scheduler vs. a
long-running supervisord service) and a reminder to check `/var/log/supervisor/`
after building or editing any service, since a clean exit code does not mean a
service is healthy.

**Surfacing scheduled agents in the workspace.** An agent the scheduler creates
now opens as its own tab in the main chat window (without stealing focus) and
blinks until you open it, so a new run is never easy to miss. The blink uses the
workspace's own accent color -- a sharp flash-then-fade on the whole clickable tab
region -- and is driven by a generic `highlight` label any agent can carry:
bumping the label's value re-blinks the tab. The tab re-blinks for each genuinely
new run whether it was left open or closed (a tab you're actively viewing is left
alone), including runs picked up at startup after downtime; per-browser state
remembers which run you've acknowledged so it neither re-opens a tab you closed nor
blinks one you've already seen. The system interface reads each agent's labels
straight from the discovery stream, so the Caretaker is reliably recognized and the
hidden services agent stays hidden.
