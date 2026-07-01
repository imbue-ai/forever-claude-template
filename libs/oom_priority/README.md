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

- **`bands`** -- the `oom_score_adj` value per band (protected = 0; user agent <
  worker agent < agent subprocess) and the helper that writes it. Bands are
  positive-only: a negative value (true "never kill") needs `CAP_SYS_RESOURCE`,
  which the container does not have, so protected processes simply keep the
  inherited default of 0 and are additionally shielded by earlyoom `--avoid`.
- **`agent_identity`** -- decides whether an agent is a user or worker agent
  (from its label), used by the launch wrapper to pick the band.
- **`registry`** -- one file per agent recording its main-process pid, so a
  killed pid can be mapped back to "which agent" (earlyoom's after-kill hook is
  handed only a pid that is already gone).
- **`ledger`** -- the append-only shed ledger and the revival-notice bookkeeping.

Tagging happens at three startup points, each setting a process's band directly
without inspecting the process tree:

| What | When | Band | Set by |
|---|---|---|---|
| supervisord services | (inherited) | protected (0) | nothing -- 0 is the default |
| an agent's main process | launch | user / worker agent | `scripts/claude_oom_launch.py` |
| an agent's subprocesses | each Bash tool call | agent subprocess (most expendable) | `scripts/claude_oom_tag_subprocess.py` (PreToolUse) |

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

Positive-only bands plus `--avoid` keep the protected processes (UI, tunnel,
terminal, backups, supervisord, sshd, tmux, earlyoom) very unlikely to be shed,
but not impossible: under sustained pressure with nothing else to kill, earlyoom
will eventually take one. Hard "never kill" protection (`oom_score_adj -1000`)
needs `CAP_SYS_RESOURCE`, which is a deferred follow-up.
