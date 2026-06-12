# Manual out-of-memory drill

The watchdog's shed ordering, banner, notices, and recovery loop can only be
*fully* verified by actually exhausting memory inside a running container and
watching tiers die in order. That is environment-dependent (and destructive), so
it is a documented manual drill rather than a CI test. Run it once per deployment
mode you care about (docker/gVisor, lima/runc, vps).

The pure logic underneath (classification, shed selection, the breaker state
machine, status/ledger IO) is covered by the unit tests next to the source and
runs in CI.

## Prerequisites

- A live workspace container with the bootstrap services up (`svc-memory-watchdog`
  window present in the services tmux session).
- A way to watch memory: `watch -n1 free -m` in a terminal.
- The system interface open in the UI.

## Procedure

1. **Baseline.** Confirm the watchdog is publishing status:

   ```bash
   cat runtime/memory_watchdog/status.json    # is_under_pressure should be false
   ```

   Confirm tagging is happening: pick an agent's pytest/chromium child PID and
   check it has a high oom_score_adj:

   ```bash
   cat /proc/<child-pid>/oom_score_adj         # expect ~900 for an agent child
   cat /proc/<system-interface-pid>/oom_score_adj  # expect 0 (protected)
   ```

2. **Create the conditions.** In a *worker* or chat agent, start a deliberately
   large but bounded memory hog as a child process (a build, a big pytest run, or
   a synthetic allocator). A simple synthetic hog:

   ```bash
   python3 -c "x=bytearray(1); 
   import time
   chunks=[]
   while True:
       chunks.append(bytearray(50_000_000))  # 50 MB at a time
       time.sleep(0.2)"
   ```

   Watch `free -m` climb toward the container limit.

3. **Observe shedding.** As usage crosses ~90% for ~10s, the watchdog should kill
   the hog (tier 8, agent child) first. Verify:

   ```bash
   tail -n 20 runtime/memory_watchdog/events/shed/events.jsonl
   ```

   You should see `process_shed` lines, lowest tier (8) first. The agent process
   itself, the system interface, bootstrap, and backups must NOT appear.

4. **Observe the banner.** The system interface should show the calm
   memory-pressure strip naming what was shed. It should clear on its own once
   usage subsides.

5. **Observe the notice.** If you push the drill hard enough that a *worker
   agent* (tier 7) gets shed, then message that agent (reviving it). Its first
   turn should carry the injected notice that it was stopped for memory and its
   background tasks were not restarted. Verify the ledger gained a
   `notice_delivered` line for it.

6. **Observe recovery.** Kill the bootstrap process to confirm the watchdog
   restarts it (mutual supervision):

   ```bash
   pkill -f "uv run bootstrap"   # find the exact PID first in practice
   # within ~2 polls the svc-... / bootstrap window should be running again
   ```

7. **Observe the breaker.** Add a service to `services.toml` whose command exits
   immediately and repeatedly (e.g. `command = "false"`, `restart = "on-failure"`).
   After a few rapid restarts bootstrap should pause it; check:

   ```bash
   grep service_blocked runtime/memory_watchdog/events/shed/events.jsonl
   ```

   and confirm the banner lists it as paused. Remove the service when done.

## Cleanup

Kill any leftover synthetic hog, remove the test service from `services.toml`,
and confirm `is_under_pressure` returns to false.
