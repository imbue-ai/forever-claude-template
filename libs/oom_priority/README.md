# oom_priority

Makes out-of-memory situations in the container degrade gracefully instead of at
the kernel's whim. The actual memory watching and killing is done by
**earlyoom** (a small C daemon, run as a supervised service); this package holds
the small amount of Python that *steers* and *records* it.

## How it fits together

earlyoom picks its victim by reading `/proc/*/oom_score`, the kernel "badness"
value -- which already folds in each process's `oom_score_adj`. So the whole
priority scheme is just: set each process's `oom_score_adj` once, at startup,
into one of a few bands.

- **`bands`** -- the `oom_score_adj` value per band and the helper that writes
  it. From least- to most-expendable: never-kill infrastructure (0) < built-in
  services (`SERVICE_BANDS`, 10-70) < user-created services (`USER_SERVICE`, 200)
  < user agent (300) < worker agent (600) < agent subprocess (900) < shared
  browser (1000). Chat agents occupy a *dynamic* sub-range, `CHAT_AGENT_FLOOR`
  (300, = an engaged chat) to `CHAT_AGENT_BASE` (560, an idle chat), always
  between the service bands and the worker band; the system_interface prioritizer
  moves a chat within it from live UI engagement (see "Dynamic chat band" below).
  Bands are positive-only: a negative value (true "never kill")
  needs `CAP_SYS_RESOURCE`, which the container does not have, so the never-kill
  infrastructure (sshd, supervisord, earlyoom, tini, tmux) simply keeps the
  inherited default of 0 and is additionally shielded by earlyoom `--avoid`. The
  service order is a best-effort steer, not a hard guarantee -- see "Protection
  is soft" below.
- **`agent_identity`** -- classifies an agent from its label (primary, chat, or
  worker), used by the launch wrapper to pick the band. An agent whose record
  can't be read matches none of these and is tagged least-protected (worker band).
- **`registry`** -- one file per agent recording its main-process pid, so a
  killed pid can be mapped back to "which agent" (earlyoom's after-kill hook is
  handed only a pid that is already gone).
- **`ledger`** -- the append-only shed ledger and the revival-notice bookkeeping.

Tagging happens at three startup points, each setting a process's band directly
without inspecting the process tree:

| What | When | Band | Set by |
|---|---|---|---|
| never-kill infra (sshd, supervisord, earlyoom, tini, tmux) | (inherited) | protected (0) | nothing -- 0 is the default, plus earlyoom `--avoid` |
| a built-in supervisord service | launch | its `SERVICE_BANDS` value | `scripts/oom_tag_service.py <service>` (command prefix) |
| a user-created supervisord service | launch | user service (above every built-in) | `scripts/oom_tag_service.py user` (command prefix) |
| an agent's main process | launch | chat -> expendable chat band (560); worker or unidentifiable -> worker agent | `scripts/claude_oom_launch.py` |
| an agent's subprocesses | each Bash tool call | agent subprocess (most expendable) | `scripts/claude_rewrite_bash_command.py` (PreToolUse; also sets the commit identity) |
| a shared browser | launch | `SHARED_BROWSER` (1000, the ceiling) | inline `oom_score_adj` write in the `browser` program |
| Chromium's own processes | on fleet events (launch, new page, navigation) | `[SHARED_BROWSER_FLOOR, SHARED_BROWSER]` (910-1000) | the browser service's re-tagging sweep (`browser.oom_retag`) -- see "The Chromium exception" below |

Each supervisord service tags itself the same way an agent's main process does:
its `command` in `supervisord.conf` runs `scripts/oom_tag_service.py <key> <the
real command>`, which sets its own `oom_score_adj` from `SERVICE_BANDS` and then
`exec`s the command in place (the band survives `execve` and is inherited by
every child). Built-in services pass their own name; a **user-created** service
(added via the `update-service` skill) passes the `user` key so it is shed before
any built-in service. An unknown key is tagged as `user` too (with a warning):
an unrecognized service must fail *expendable*, never protected.

A **backstop event listener** (`scripts/oom_tag_backstop.py`, the
`oom-tag-backstop` supervisord program) covers the one case the prefix cannot: a
service whose command omits the wrapper entirely, which would otherwise keep the
inherited `oom_score_adj` of 0 and sit as protected as sshd/supervisord. On
every `PROCESS_STATE_RUNNING` event (boot and each restart) it resolves the
program's expected band by *program name* (`bands.supervisord_program_band`: a
built-in's own band; `USER_SERVICE` for anything unrecognized) and raises the
process -- plus any children it already spawned, found via a
`/proc/<pid>/task/*/children` walk -- up to that band. It only ever raises,
never lowers, so a self-tagged process (the browser at the ceiling) and the
`PROTECTED` programs (earlyoom, deferred-install, the listener itself) are never
demoted. The prefix remains the primary mechanism because it tags at spawn:
the RUNNING event fires only after `startsecs` (~1s), leaving a short window
where an unwrapped service runs untagged.

The agent's main process tags *itself*: the `claude` and `worker` agent types'
`command` (in `.mngr/settings.toml`) runs `scripts/claude_oom_launch.py`, which
sets its own `oom_score_adj` to the agent band, records its pid, then `exec`s
claude in place. (Both the `claude` and `worker` types set the command. The
`worker` type has to repeat it rather than inherit it from `claude` because of an
mngr config-load bug: `load_config` ends with a `MngrConfig.model_validate` that
re-marks every agent-type field as explicitly set, so `resolve_agent_type`'s
`parent_type` inheritance treats a child's defaulted `command` as set and clobbers
the parent's value. The config resolver inherits correctly in isolation -- only
the full load path breaks it -- so a worker without this line launches plain
claude and never gets its band. Setting it on both types is the reliable fix.)
Because the band and pid survive `execve`, the tagged process *is* the claude
process, so its band is set before any subprocess exists. A subprocess inherits its
agent's band by default; the PreToolUse hook raises it the rest of the way so a
runaway build/test/browser is always shed first.

## The Chromium exception

Everything above rests on inheritance: tag a process once and its whole subtree
keeps the band. Chromium is the one process in the workspace that breaks this.
Each Chromium process overwrites any inherited `oom_score_adj` once at its own
startup with Chrome's internal gradation (browser/zygote 0, gpu/utility 200,
renderers 300 -- `AdjustLinuxOOMScore` in chromium's `chrome_main_delegate.cc`,
with no flag to disable it). So the browser daemon's ceiling tag survives only
on processes that never self-write (the node/Playwright driver, crashpad), while
the memory-heavy renderers end up at 300 -- *more* protected than workers (600)
and agent subprocesses (900), inverting the design.

The kernel cannot forbid the lowering: without `CAP_SYS_RESOURCE` any process
may lower its own value back down to its inherited floor (`oom_score_adj_min`,
0 everywhere in this container). But Chromium writes each value exactly once
(its continuous re-adjustment is ChromeOS-only), so an external raise sticks.
The browser service therefore sweeps its descendants and remaps every value
found below `SHARED_BROWSER_FLOOR` (910) into `[SHARED_BROWSER_FLOOR,
SHARED_BROWSER]` via `bands.shared_browser_oom_score_adj`. The mapping is
order-preserving, so Chrome's gradation survives in compressed form -- worth
keeping, because it means earlyoom sheds one tab's renderer before the whole
browser. The sweep only remaps values below the floor, so it is idempotent and
never touches the inherited-ceiling processes.

The sweep is event-driven, not periodic: Chromium processes appear only at
moments the fleet observes -- a browser launch, a new page (the CDP observer's
`page` event fires for every new tab, whether opened by an agent command, a
human in the cast viewer, or a page popup), and a navigation (`framenavigated`
fires for every frame, and a cross-site navigation can swap in a fresh
renderer). Each such event triggers a short burst of sweeps (`browser.oom_retag`,
~1s cadence for ~6s), because the processes spawn and self-write their values
over the seconds *after* the event; between events the sweeper sleeps.

## Dynamic chat band

Every agent's band is set once at launch and never changes -- with one exception:
**chat agents**. A chat is a user-facing agent (`user_created` label), and how
expendable it should be depends on how engaged the user is with it, which is only
known at runtime. So the launch wrapper tags a chat at `CHAT_AGENT_BASE` (the
*most*-expendable chat band, 560), and the system_interface `ChatOomPrioritizer`
re-tags it downward toward the protected floor (`CHAT_AGENT_FLOOR`, 300) as the
user engages: `oom_score_adj` is a function of whether the chat's tab is open,
whether it is visible, and how recently it was messaged relative to other chats.

Starting at the expendable end is deliberate: a chat is only ever *protected* by a
reported engagement, so a chat nobody is engaging with (dormant, or messaged
outside the UI) stays maximally expendable rather than over-protected -- and a
shed chat just revives on its next message. For the same reason, an agent whose
record can't be classified falls through to the worker band, not a protected one.

Re-tagging is purely event-driven: the prioritizer runs on each `/api/activity`
report the frontend posts (on tab-presence changes and after a message send), with
no polling. That is race-free for the revive-on-message path because the send
blocks until the revived process is ready -- the wrapper registers its pid before
`exec`, so the pid exists by the time the frontend reports activity after the send
returns. (The system interface still runs a short lifecycle poll, but only to feed
the UI's liveness dot; it no longer drives OOM re-tagging.)

## Outputs

- **Shed ledger** (`runtime/oom_priority/events/shed.jsonl`): append-only,
  written by `scripts/earlyoom_record_shed.py` (earlyoom's `-N` after-kill hook).
  One `process_shed` line per kill, carrying the agent name only when an agent's
  *own* process was shed. Read by the revival-notice hook
  (`scripts/claude_shed_notice_hook.py`) and the launch-task report poll.
- **Agent-pid registry** (`runtime/oom_priority/agent_pids/<pid>.json`): written
  by the launch wrapper (`scripts/claude_oom_launch.py`), read by the kill hook.

Both live under `runtime/` so they ride the runtime-backup branch. Their absolute
location is pinned via `OOM_PRIORITY_RUNTIME_DIR` (see `.mngr/settings.toml`) so
the container-level kill hook and every agent's per-worktree hooks resolve the
same files. `paths` is the single source of truth for the layout, and -- like
every module here -- is stdlib-only, so the hooks (which run under a plain
`python3`, not `uv`) can import it via a `sys.path` insert.

## Protection is soft

Two things here are best-effort, not hard guarantees:

- **The never-kill infrastructure isn't truly immortal.** Positive-only bands
  plus `--avoid` keep sshd, supervisord, earlyoom, tini, and tmux very unlikely
  to be shed, but under sustained pressure with nothing else to kill earlyoom
  will eventually take one. Hard "never kill" protection (`oom_score_adj -1000`)
  needs `CAP_SYS_RESOURCE`, which the container does not grant -- a deferred
  follow-up.
- **The service ordering can be reordered by memory usage.** earlyoom picks the
  highest `/proc/*/oom_score`, which adds each process's live memory badness on
  top of its `oom_score_adj`. The service bands are only ~10 apart, so a service
  using enough more memory than the one below it can outweigh the band gap and be
  shed first. The bands guarantee the ordering only when memory usage is
  comparable; in the common case the services are lightweight and the order
  holds. Widening the gaps would need to push the top service bands past the
  agent bands, which would defeat the "services outlive agents" goal, so the
  bands stay a steer rather than a strict priority.
