# OOM prioritization and graceful degradation

## Overview

- When the container runs out of memory, the kernel picks a victim on its own — usually whatever is biggest (a claude agent, a pytest run, Chromium). The kill is silent: nothing is recorded, nobody is told, and there is no guided recovery. In the worst case the kernel takes out the user's window into the system (web UI, tunnel, terminal) and they lose visibility entirely.
- Use **earlyoom** (a small, battle-tested C OOM-prevention daemon, baked into the Docker image) to act *before* the kernel does: it watches memory and, under pressure, sends `SIGTERM` (then `SIGKILL` under deeper pressure) to the process the kernel would consider worst, read from `/proc/*/oom_score`.
- Steer earlyoom's choice by setting each process's `oom_score_adj` **once at startup** into a few coarse priority bands: the user's window-in and the recovery machinery stay protected, agents are expendable, and an agent's heavy subprocesses are the most expendable. earlyoom then kills within that ordering, further biased by actual memory use (the real hog goes first).
- No re-scanning loop is needed because a process's band never changes: services tag themselves at launch, agents tag themselves (and register their pid) at session start, an agent's subprocesses tag themselves via a Bash hook, and every child inherits its parent's band.
- Everything is recorded to an append-only **shed ledger**. A shed user agent revives on the user's next message with an injected notice; a shed worker is discovered by its lead through the existing report-poll path.

## Expected behavior

- Under normal memory conditions nothing is visible: earlyoom idles and each process sits at its startup band.
- Under real pressure earlyoom `SIGTERM`s the worst-ranked process (most-expendable band first, biggest first within a band), re-checks, and continues only while memory stays low; under deeper pressure it escalates to `SIGKILL`. It acts on instantaneous pressure each poll, and `SIGTERM`-first keeps a brief spike recoverable.
- The most-expendable band holds **every agent's build / test / browser subprocesses**, so a runaway pytest or Chromium is sacrificed first; worker agents go before user agents; the user's UI, tunnel, terminal, backups, and earlyoom itself are protected and are only ever killed if nothing else remains.
- A shed **user-created agent** stays down until the user next messages it. On revival a hook injects a notice — it was stopped to relieve memory pressure, and its background tasks (e.g. polling loops) were cancelled and not restarted — so it re-checks state before continuing rather than assuming its last action finished.
- A shed **worker** is found by its lead through the existing launch-task report-poll, which also watches the ledger and surfaces the pause promptly (instead of waiting out the full timeout). The lead revives it once with `mngr start <worker> --restart` and nudges it to continue; a worker shed twice escalates to the user.
- Every kill appends one ledger line. Under the runc runtime (lima), if the kernel's own OOM killer beats earlyoom it still follows the same `oom_score_adj` ordering. Under gVisor earlyoom is the only ordered mechanism; if a hard OOM still wins the sandbox dies and the outer host-restart layer brings the container back (existing behavior, outside this work).

## Changes

- **earlyoom service.** Install earlyoom in the Docker image and run it as a supervised background service with tuned thresholds (`-m` available-memory percent for `SIGTERM`/`SIGKILL`, `-s` swap percent) and `--avoid` for the few protected processes that have distinct, stable names. It runs an after-kill hook on each kill.
- **Priority bands.** Define the `oom_score_adj` bands as tunable constants: protected = 0 (tmux / sshd / entrypoint, system interface, tunnel, terminal, backups, supervisord, earlyoom itself); then increasing positive values for user agents < worker agents < agent subprocesses.
- **Self-tag at startup**, three injection points (raising `oom_score_adj` is unprivileged, so no extra capabilities are required):
  - *Services*: a small wrapper sets the service's band before `exec`ing it; its children inherit.
  - *Agents*: a SessionStart hook sets the agent's own process to its band — user vs worker, chosen from the agent's label — and registers its pid → agent name.
  - *Agent subprocesses*: a PreToolUse Bash hook rewrites the command to raise the shell to the most-expendable band; everything it spawns inherits.
- **Agent labels.** Tag UI and bootstrap chat-creation paths `user_created=true` and worker creations `agent_created=true`, so the SessionStart tagger picks the right band.
- **Shed ledger.** Append-only, under `runtime/` so it rides the runtime-backup branch. The after-kill hook maps the killed pid to an agent via the startup registry and writes the record (the agent name is set only when an agent's *own* process was killed, which is what drives revival).
- **Revival notice.** A SessionStart hook injects any pending shed notice for this agent from the ledger, then marks it delivered so it is not repeated.
- **launch-task integration.** The lead's report-poll also watches the ledger and surfaces a shed worker promptly; the dead-worker-recovery guidance consults the ledger before reviving (revive at most once; a twice-shed worker escalates to the user).
- **Verification.** A documented manual OOM drill (run a memory hog, observe kill order, the ledger, the revival notice, recovery). Unit tests cover band assignment, the after-kill pid→agent ledger mapping, and the revival-notice logic; real-container testing is performed by the user.
- **Explicitly deferred / out of scope:**
  - A live in-UI indication of memory pressure or recent shedding — for now the surfaces are the ledger, the revival notice, and earlyoom's own logs (a better live indicator is a follow-up).
  - Hard "never kill" protection of the protected band via `oom_score_adj -1000`, which requires `CAP_SYS_RESOURCE` (the container's default capability set lacks it); the related gap of hard-protecting a user agent's heavy subprocesses from outranking a worker agent is folded into this same follow-up. Protection is soft for now (bands + `--avoid`).
  - An on-demand current-pressure check (reading `/proc/meminfo`) before reviving; for now revival follows the "revive at most once, twice-shed escalates" rule without a live pressure gate.
  - Auto-revive of shed agents once pressure clears, and any watcher that decides what to bring back; revival stays user-driven.
  - A sustained-pressure duration gate (earlyoom acts on instantaneous pressure), whole-process-group kills (`-g`), and container-death recovery for headless deployments — the outer host-restart tier already covers container death where a UI is attached.

## Follow-ups (post-implementation notes)

The sections above are the plan as originally written. This records where the shipped implementation diverged from it:

- **Agent band tagging moved from a SessionStart hook to the launch command.** The plan (see "Self-tag at startup" and "Agent labels") tagged an agent's own process via a SessionStart hook. As built, the agent tags itself in a launch wrapper (`scripts/claude_oom_launch.py`) set as the `claude`/`worker` agent types' `command` in `.mngr/settings.toml`: it sets its own `oom_score_adj`, registers its pid, then `exec`s the real `claude` in place. Because the band is set at launch rather than at session start, it is in effect before any subprocess is spawned, and there is no need to locate the claude process after the fact. The revival-notice injection (see "Revival notice") remains a SessionStart hook. See `libs/oom_priority/README.md` for the current design.
- **The wrapper `command` is set on both the `claude` and `worker` agent types.** The `worker` type repeats it rather than inheriting from `claude` because an mngr config-load quirk makes `parent_type` inheritance treat a child's defaulted `command` as explicitly set, clobbering the parent's value; setting it on both types is the reliable fix. See the README for details.
