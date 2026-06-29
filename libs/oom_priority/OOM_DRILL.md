# OOM drill

A manual walkthrough to confirm the container degrades gracefully under memory
pressure: that earlyoom sheds the most-expendable work first, the kill is
recorded, and a shed agent is told it was paused when it comes back. Run it on a
real container (not macOS -- the tagging needs `/proc`).

## 0. Confirm earlyoom is running

```bash
supervisorctl status earlyoom         # RUNNING
pgrep -a earlyoom                     # one process, with the -m/-s/-N/--avoid args
```

## 1. Confirm the priority bands are set

Pick a live agent and its claude process, and a subprocess it spawned (run a
`sleep 600 &` from the agent's terminal first):

```bash
# The agent's main process should sit at its band (300 user / 600 worker);
# a subprocess it spawned via the Bash tool should sit at 900.
cat /proc/<claude_pid>/oom_score_adj          # 300 or 600
cat /proc/<subprocess_pid>/oom_score_adj      # 900
# A protected service stays at 0:
cat /proc/$(pgrep -f system-interface | head -1)/oom_score_adj   # 0
```

## 2. Trigger a shed with a memory hog

From an agent's terminal, allocate more than the free headroom so earlyoom
crosses its threshold. A subprocess (tier 900) should be the victim, not the
agent or any service:

```bash
# Allocates ~2 GiB and holds it. Adjust the count to exceed free memory.
python3 -c "x=bytearray(2*1024*1024*1024); import time; time.sleep(600)"
```

Watch earlyoom act:

```bash
supervisorctl tail -f earlyoom stderr
# expect: sending SIGTERM to process <pid> ... "python3": oom_score ...
```

## 3. Confirm the kill was recorded

```bash
tail -n 5 /mngr/code/runtime/oom_priority/events/shed.jsonl
# the python3 hog -> a process_shed line; agent_name null (it was a subprocess).
```

## 4. Confirm an agent shed gets a revival notice

To exercise the agent path, raise an idle agent's claude process so it is the
worst, then let earlyoom take it under pressure (or, for a pure
notice-plumbing check, append a shed record for a real agent name and message
that agent):

```bash
# After the agent's own process is shed, message it (or open its chat). On
# session start it should print the "you were previously stopped to relieve a
# memory-pressure situation" notice exactly once.
grep '"agent_name": *"<agent>"' /mngr/code/runtime/oom_priority/events/shed.jsonl
mngr start <agent> --restart && mngr message <agent> -m continue
```

A shed worker is surfaced to its lead automatically: the launch-task `await`
poll returns exit code 75 with revive instructions (see
`.agents/skills/launch-task/references/dead-worker-recovery.md`).

## 5. Confirm the protected processes survived

```bash
supervisorctl status        # system_interface, cloudflared, terminal, backups: RUNNING
```

Nothing in tiers protected/UI/recovery should appear as a victim in the earlyoom
log or the ledger unless the container was so far gone that no expendable work
remained.
