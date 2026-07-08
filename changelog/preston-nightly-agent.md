Recurring jobs via cron/anacron, and the Caretaker. Adds recurring-job scheduling
to every workspace using the stock OS tools, a daily **Caretaker** agent built on
top of it, and the supporting skills, docs, and in-workspace tab behavior that
make scheduled agents visible.

**Recurring jobs with cron + anacron.** Workspaces schedule recurring work with
the standard OS tools instead of a custom service: **anacron**
(`/etc/anacrontab`) for jobs that run about once a day or coarser and must not be
skipped when the container was off -- it catches up missed runs at the next
opportunity, coalescing several misses into one run -- and **cron**
(`/etc/cron.d/` drop-ins) for precise times or sub-daily cadences, exact but
never backfilled. The cron daemon runs under supervisord (`[program:cron]`);
anacron has a single deliberately-simple trigger, a cron line firing every
minute between 03:00 and 23:59 (`/etc/cron.d/fct-anacron`), so daily jobs run
at 3 AM local time and a job missed while the container was off starts within
a minute of the first in-window boot. The Caretaker never runs at workspace
creation: the bootstrap seeds its anacron stamp at first boot, so its first run
is the next day's 3 AM -- and when the user's timezone cannot be fetched, the
bootstrap adopts a fixed-offset zone that lands that first run about 8 hours
after setup instead. Because cron and
anacron scrub the job environment, a small wrapper
(`scripts/with_agent_env.sh`) restores the workspace environment from a snapshot
the bootstrap writes each boot, and every scheduled job runs through it. The
container's clock is set to the user's local timezone at each boot: the
bootstrap pulls it from the minds desktop client's `GET /api/v1/timezone`
through the latchkey gateway (falling back to UTC when unreachable), so
schedules run in the user's local time. (An earlier iteration of this branch
built a custom `libs/scheduler` service for the catch-up behavior; it was
removed in favor of anacron, which provides the same missed-run semantics with
zero code to maintain -- the trade-off being that anacron is day-granularity, so
the Caretaker runs "once a day, shortly after local midnight or on the first
boot of the day" rather than at a fixed 3 AM.)

**Scheduled agent tasks and the Caretaker.** A scheduled job can wake an agent
that runs a skill on a cadence, in its own chat tab. `scripts/run_task_agent.sh
<skill>` spawns a single persistent agent for that skill; on each run mngr clears
the agent's session and re-sends `/<skill>` so the skill runs fresh, with no memory
of the previous run.
A new scheduled agent (e.g. a morning news digest) needs only a skill plus an
anacron or cron entry -- no new agent template. The daily **Caretaker** is the
built-in instance, baked into `/etc/anacrontab` at image build (delete its line
to switch it off): once a night it quietly checks the apps and services in your
workspace for problems -- a page that stopped loading, a service that crashed,
errors piling up -- and either fixes them or explains what it found, always in
plain, non-technical language. On its very first night it does one look-only scan
(changing nothing), then introduces itself with what it found and asks whether to
keep checking each night, fix small things on its own, or be switched off; from
the second night on it scans only once you've opted in. It greets you each night
before it starts, runs each night from a fresh session (no memory of the prior
run), and remembers your choices and what it saw on previous nights through its own
notes on disk, not the conversation. Your standing permissions live in a single plain-language
`runtime/caretaker/permissions.md` that the Caretaker reads each run and rewrites
when you change your mind, and that you can edit yourself any time. You stay in
full control: change when it runs, give it other regular chores, or switch it off
entirely.

**Health-check skills and docs.** Adds a `check-app-errors` skill (survey
`supervisorctl status`, scan `/var/log/supervisor/` for errors and tracebacks,
summarize what's wrong and where), reusable by both day-to-day chat agents and the
Caretaker's nightly scan; and a `manage-scheduled-tasks` skill that teaches agents
to choose anacron vs. cron per job, the entry formats, the env wrapper, and to
re-check the user's current timezone (via the minds timezone endpoint) before
scheduling anything, updating the container clock if the user has moved.
The full scheduling detail (anacron vs. cron, entry formats, the
env wrapper, the timezone check, the Caretaker wiring) lives in the
manage-scheduled-tasks skill; CLAUDE.md gains just one sentence pointing at the
manage-scheduled-tasks and check-app-errors skills.

**Surfacing scheduled agents in the workspace.** An agent a scheduled job creates
now opens as its own tab in the main chat window (without stealing focus) and
blinks until you open it, so a new run is never easy to miss. The blink is a
yellow flash-then-fade on the whole clickable tab region, driven by a generic
`highlight` label any agent can carry:
bumping the label's value re-blinks the tab. The tab re-blinks for each genuinely
new run whether it was left open or closed (a tab you're actively viewing is left
alone). Surfacing is driven entirely by one persisted signal -- the run you last
acknowledged (by viewing or closing the tab) versus the run currently showing --
so it is idempotent on reconnect: a run that fired while your laptop was asleep
(e.g. the nightly Caretaker at 3 AM) surfaces and blinks the moment the workspace's
web UI reconnects, with no need to reopen anything. Closing a blinking tab dismisses
that run (it will not immediately reopen), and a genuinely newer run brings it back.
The system interface reads each agent's labels straight from the discovery stream,
so the Caretaker is reliably recognized and the hidden services agent stays hidden.
