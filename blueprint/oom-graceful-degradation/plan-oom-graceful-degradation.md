# OOM prioritization and graceful degradation

## Overview

- Today, when a container built from this repo runs out of memory, victim selection is at the kernel's whim — often the biggest process (a claude agent, a pytest run, Chromium) dies silently, and nothing records it, notifies anyone, or guides recovery. In the worst case the kernel takes out tmux, the system interface, or the tunnel, and the user loses their window into the system entirely.
- The design assigns every process in the container to a priority tier via one shared classifier, consumed by two mechanisms: an `oom_score_adj` tagger (steers the kernel's own OOM killer where the kernel honors it — runc/lima) and a shedder watchdog (works in every mode, including gVisor where in-container `oom_score_adj` cannot steer the host's victim selection and a hard OOM kills the whole sandbox).
- The shedder polls `/proc/meminfo` and, under sustained pressure, kills whole tiers from the bottom up — most-expendable work dies first, the user's window in (system_interface, cloudflared, ttyd) and the recovery machinery are never shed.
- Everything is recorded: a shed-event ledger plus a continuously updated status file under `runtime/` (backed up, and the raw data behind the UI banner, agent notices, and revival decisions).
- Recovery is a closed loop: bootstrap and the watchdog supervise each other, the watchdog also covers telegram/ttyd, bootstrap gets a crash-loop circuit breaker, and shed agents revive on the next user message with an injected notice. Whole-container death is already auto-recovered by the minds desktop client's host-restart tier (verified by exploring `vendor/mngr/apps/minds`; that outer layer only fires when the UI is unreachable, so the two mechanisms are complementary, not redundant).

### Tier table (most protected first)

| Tier | Members | adj | Sheddable |
|---|---|---|---|
| 1 | tmux server, sshd, container entrypoint | 0 | never |
| 2 | system_interface, cloudflared, ttyd | 0 | never |
| 3 | bootstrap, watchdog | 0 | never |
| 4 | runtime-backup, host-backup | 0 | never |
| 5 | user-created agents (`user_created=true` label; unlabeled agents default here) | + | last resort |
| 6 | telegram-bot, web, app-watcher, and all agent-added services.toml services | ++ | yes |
| 7 | agent-created agents (workers etc.) | +++ | yes |
| 8 | agent children: builds, tests, Chromium, pollers (no exemptions) | ++++ | shed first |

- Protected tiers stay at `oom_score_adj` 0 and expendable tiers get increasingly positive values — negative values would require `CAP_SYS_RESOURCE`, which Docker's default cap set does not grant; positive-only tagging achieves the same relative ordering without extra capabilities.

## Expected behavior

- Under normal memory conditions nothing is visible: the tagger keeps `oom_score_adj` values current as processes come and go, and the status file reports healthy.
- Under sustained pressure (defaults to be tuned: ~90% usage held for ~10s, exposed as constants), the shedder kills tier 8 in its entirety, re-evaluates, and escalates tier by tier — 8, then 7, then 6, then 5. Tiers 1-4 are never shed.
- Every kill and every pause is appended to the shed-event ledger; the status file (current usage, threshold, last-shed summary) is rewritten every poll as the read API for the banner, for agents checking pressure before heavy work, and for revival decisions.
- A shed user-created agent stays dead until the user next messages it (which is what revives agents today). On revival, a hook injects the queued notice — it was killed to relieve memory pressure, and its background tasks (e.g. polling loops) were cancelled and not restarted — then clears the pending notice.
- A shed worker is discovered by its parent through the existing launch-task flow (report-poll timeout, then liveness diagnosis); no new notification mechanism, and nothing impersonates the worker-report contract. The dead-worker-recovery reference gains a step: check the ledger, and follow the revival guidelines — do not revive while pressure is elevated (surface to the user instead), revive at most once after pressure clears, and a twice-shed worker always escalates to the user.
- A config-driven auto-revive list (default empty) names agents the watchdog re-messages once pressure clears; everything else stays down until explicitly revived. The intelligent "watcher agent" that decides what to bring back is deferred follow-up work.
- A service that fails rapidly N times in a row trips bootstrap's circuit breaker: restarts pause for a cooldown, the service is marked blocked in the ledger, and it resumes after the cooldown.
- system_interface shows a calm, non-alarming banner during sustained pressure or recent shedding, listing what was shed and which services are paused; it disappears when pressure clears.
- Supervision is mutual: bootstrap restarts the watchdog via the existing services.toml `on-failure` policy; the watchdog restarts bootstrap, telegram-bot, and ttyd if their processes die (preserving ttyd's terminal-survives-bootstrap-failure property).
- Under runc (lima), a kernel OOM kill that beats the shedder still follows tier order thanks to the adj tags. Under gVisor, the shedder is the only ordered mechanism; if it loses the race, the sandbox dies and the minds host-restart tier brings the container back (existing behavior, outside this plan).
- Headless deployments (no minds desktop client attached — e.g. telegram-only) have no outer container-restart layer; that gap is explicitly deferred.

## Changes

- New watchdog service, registered in services.toml with `restart = "on-failure"`: process classifier (tier assignment from process ancestry, tmux session/window mapping, and agent labels), `oom_score_adj` sweep, pressure monitor, whole-tier shedder, ledger + status file writer, supervision of bootstrap/telegram-bot/ttyd, and auto-revive list processing.
- bootstrap: restart backoff and crash-loop circuit breaker, with blocked-service state written to the ledger.
- system_interface: memory-pressure banner fed from the watchdog's status file and ledger.
- Agent creation paths: add the `user_created=true` label to UI chat creates and the bootstrap initial chat (UI worktree creates already set it); workers and other agent-created paths get no label and classify as tier 7; unlabeled agents default protectively to tier 5.
- Claude Code hook: on agent revival, inject any pending shed notice from the ledger as context, then clear it.
- `.agents/shared/references/dead-worker-recovery.md`: add the ledger check and the revival guidelines.
- A documented manual OOM drill (run a memory hog, observe shed order, banner, notices, recovery); unit tests cover classifier, shedder, and breaker logic — real-container testing is performed by the user.
- Explicitly out of scope: `--restart` docker args (minds' host-restart tier covers container death), recovery-page memory-pressure probe in vendor/mngr (avoids further outer-app/container coupling), headless container-death recovery, per-service tier configuration, and any auto-restart of shed background work.
