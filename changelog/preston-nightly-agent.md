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
the job environment, a small wrapper (`scripts/with_agent_env.sh`) restores
the workspace environment from a snapshot the bootstrap writes each boot, and
every scheduled job runs through it. The container's clock is set to the
user's local timezone at each boot: the bootstrap pulls it from the minds
desktop client's `GET /api/v1/timezone` through the latchkey gateway (falling
back to UTC when unreachable), so schedules run in the user's local time. (An
earlier iteration of this branch built a custom `libs/scheduler` service --
about 635 lines -- for the catch-up behavior; the checker replaced it with
about 50 lines of shell.)

**Scheduled agent tasks and the Caretaker.** A scheduled job can wake an agent
that runs a skill on a cadence, in its own chat tab. `scripts/run_task_agent.sh
<skill>` spawns a single persistent agent for that skill; on each run mngr clears
the agent's session and re-sends `/<skill>` so the skill runs fresh, with no memory
of the previous run.
A new scheduled agent (e.g. a morning news digest) needs only a skill plus a
cron entry -- no new agent template. The weekly **Caretaker** is the built-in
instance -- and it is **off by default, as a BETA feature**. Its cron entry
(baked into `/etc/cron.d/minds-caretaker` at image build) ticks a
deterministic gate, `scripts/caretaker_check.sh`, that is a no-op until the
user enables the feature; the new **enable-caretaker** skill (used only when
the user explicitly asks) explains the beta status, gets an explicit yes, and
turns it on by creating `runtime/caretaker/enabled`. Once enabled, the agent
introduces itself shortly afterwards, and from then on the gate runs a weekly
deterministic check -- services in FATAL/BACKOFF, fresh error output in the
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
equally short **disable-caretaker** skill (`rm runtime/caretaker/enabled`)
switches it off entirely.

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
(e.g. an overnight Caretaker run) surfaces and blinks the moment the workspace's
web UI reconnects, with no need to reopen anything. Closing a blinking tab dismisses
that run (it will not immediately reopen), and a genuinely newer run brings it back.
The system interface reads each agent's labels straight from the discovery stream,
so the Caretaker is reliably recognized and the hidden services agent stays hidden.

**Reconnecting on wake so overnight runs actually surface.** The workspace UI
now reconnects its live-updates WebSocket whenever the machine wakes from
sleep, the window refocuses, or the network returns. That connection rides an
SSH tunnel from the webview to the workspace's system interface; on sleep the
tunnel dies but the browser never fires a close event -- it leaves the socket
in a phantom "OPEN" state -- so the close-driven auto-reconnect never ran, and
a Caretaker run that fired overnight stayed invisible until a manual reload.
Returning to the workspace now drops the stale socket and opens a fresh one,
whose snapshot re-surfaces (and blinks) anything missed. Because reconnects
replay pending proto-agent creations but not completions, the reconnect also
rebuilds the local proto-agent set from the fresh snapshot, so an agent that
finished building while the laptop slept can no longer strand a tab on
"Creating agent...". A server heartbeat plus client silence-watchdog (for a
connection that dies while the window stays open and focused -- only a real
concern for remote-host workspaces) remains a known follow-up.

**Fixed: a fresh mind could deadlock on "No events yet" with no way to sign in.**
When a workspace's first boot ran with no Claude credentials, claude sat at its
interactive login screen and never signalled ready, so the bootstrap's
`mngr create ... --message /welcome` timed out and exited nonzero after the agent
was already registered. That single failure starved everything downstream: the
transcript stayed empty, so the login modal -- which only opened in reaction to
an auth-error transcript event -- could never appear; the welcome-resend had no
recorded agent id to target; and every retry on later boots collided with the
half-created agent's name and failed forever. Two changes unwind the deadlock.
The bootstrap now looks up an existing agent named after the host before
creating (the same lookup-first shape scripts/run_task_agent.sh uses): a
survivor from a partial create is adopted -- its id persisted for the welcome
resend, the signal written -- instead of colliding. And the chat panel now
backstops the login modal: when a snapshot loads with zero events, it probes
`GET /api/claude-auth/status` (previously unused) once per page load and opens
the sign-in modal if logged out. After sign-in, the existing auth-success
resend delivers the welcome as designed.
