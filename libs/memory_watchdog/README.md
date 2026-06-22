# memory_watchdog

Background service that keeps the container's memory usage survivable and makes
out-of-memory situations degrade gracefully instead of at the kernel's whim.

Every few seconds it:

1. Snapshots the process tree (`/proc`), the tmux panes, and the host's agent
   labels, then classifies every process into one of eight OOM-priority tiers
   (see `data_types.Tier`).
2. Writes each process's `oom_score_adj` to match its tier, so that under the
   runc runtime (lima) the kernel's own OOM killer picks the most expendable
   work first. Under gVisor the kernel ignores this, which is why step 3 exists.
3. If memory usage stays above the shed threshold for long enough, sheds whole
   tiers from the most expendable up -- agent build/test/browser subprocesses
   first, auxiliary services next, worker agents next, and the user's own agents
   only as a last resort. Infrastructure, the UI, the recovery machinery, and
   the backups (tiers 1-4) are never shed.

The watchdog only *decides what to shed*; it does not supervise other processes.
Liveness is owned by supervisord (see `supervisord.conf`): supervisord restarts
this watchdog if it dies, and restarts any service the watchdog sheds.

## Outputs

- **Shed ledger** (`runtime/memory_watchdog/events/shed/events.jsonl`):
  append-only record of every kill. Backed up via the runtime-backup branch.
  Consumed by the revival-notice hook and the dead-worker-recovery guidance.
- **Status file** (`runtime/memory_watchdog/status.json`): current usage,
  threshold, whether the banner should show, what was shed recently, and which
  services are blocked. The system interface reads this to render its
  memory-pressure banner. (`blocked_services` is currently always empty -- it is
  reserved for a future `supervisorctl`-driven crash-loop signal; see below.)

## Tiers

| Tier | Rank | Members | Shed |
|---|---|---|---|
| INFRASTRUCTURE | 1 | tmux server, sshd, entrypoint, pane shells, supervisord | never |
| USER_INTERFACE | 2 | system_interface, cloudflared, ttyd | never |
| RECOVERY | 3 | bootstrap, this watchdog | never |
| DURABILITY | 4 | runtime-backup, host-backup | never |
| USER_AGENT | 5 | user-created agents (and unlabeled agents) | last resort |
| AUXILIARY_SERVICE | 6 | web, app-watcher, agent-added services | yes |
| WORKER_AGENT | 7 | agent-created agents (workers) | yes |
| AGENT_CHILD | 8 | an agent's build/test/browser subprocesses | first |

## How services are classified

Background services run as `[program:*]` children of supervisord, which itself
runs in the `bootstrap` tmux pane. So a service is not its own tmux window --
it is a process in supervisord's subtree, identified by its command line (e.g.
`uv run web-server`, `system-interface`, `bash scripts/run_ttyd.sh`). The
classifier matches each supervisord child's command to a tier; anything it does
not recognize defaults to AUXILIARY_SERVICE (tier 6), so an agent-added program
is shed before worker agents but after the recognized infrastructure. Agent
sessions (separate `mngr-<name>` tmux sessions) are tiered by their agent label
(user-created vs worker), independent of supervisord.

## Crash-loop visibility (reserved)

Under the previous service manager, the bootstrap restart loop tripped a
crash-loop breaker and recorded `blocked`/`unblocked` services to the ledger so
the banner could surface a thrashing service. supervisord now owns restarts
(`autorestart` + `startretries`), so nothing writes those records today and
`blocked_services` stays empty. The ledger's block/unblock writers remain in
place so a future poller (reading `supervisorctl status` for BACKOFF/FATAL
programs) can repopulate the banner without re-plumbing.

## Paths

`memory_watchdog.ledger.shed_ledger_path()` / `status_path()` are the single
source of truth for the on-disk layout, imported by the system interface (status
reader). The base resolves relative to `MNGR_AGENT_WORK_DIR` (the repo root) and
is overridable via `MEMORY_WATCHDOG_RUNTIME_DIR`. The SessionStart notice hook
(`scripts/claude_shed_notice_hook.py`) duplicates this layout because it runs in
a plain claude environment that cannot import the package, but it honors the same
work-dir base and override so it never resolves to a different file.

## CLI

- `memory-watchdog` -- run the watchdog loop (started and supervised by
  supervisord via the `[program:memory-watchdog]` entry in `supervisord.conf`).
