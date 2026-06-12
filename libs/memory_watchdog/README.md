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

It also supervises the `bootstrap`, `telegram`, and `terminal` windows, relaunching
any whose process has died -- the mirror of bootstrap restarting this watchdog,
which closes the recovery loop.

## Outputs

- **Shed ledger** (`runtime/memory_watchdog/events/shed/events.jsonl`):
  append-only record of every kill and every service bootstrap pauses. Backed up
  via the runtime-backup branch. Consumed by the revival-notice hook and the
  dead-worker-recovery guidance.
- **Status file** (`runtime/memory_watchdog/status.json`): current usage,
  threshold, whether the banner should show, what was shed recently, and which
  services are blocked. The system interface reads this to render its
  memory-pressure banner.

## Tiers

| Tier | Rank | Members | Shed |
|---|---|---|---|
| INFRASTRUCTURE | 1 | tmux server, sshd, entrypoint, pane shells | never |
| USER_INTERFACE | 2 | system_interface, cloudflared, ttyd | never |
| RECOVERY | 3 | bootstrap, this watchdog | never |
| DURABILITY | 4 | runtime-backup, host-backup | never |
| USER_AGENT | 5 | user-created agents (and unlabeled agents) | last resort |
| AUXILIARY_SERVICE | 6 | telegram, web, app-watcher, agent-added services | yes |
| WORKER_AGENT | 7 | agent-created agents (workers) | yes |
| AGENT_CHILD | 8 | an agent's build/test/browser subprocesses | first |

## CLI

- `memory-watchdog` -- run the watchdog loop (started by bootstrap via
  services.toml with `restart = "on-failure"`).
