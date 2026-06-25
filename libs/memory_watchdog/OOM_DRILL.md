# Manual out-of-memory drill

The watchdog's shed ordering, banner, notices, and recovery loop can only be
*fully* verified by actually exhausting memory inside a running container and
watching tiers die in order. That is environment-dependent (and destructive), so
it is a documented manual drill rather than a CI test. Run it once per deployment
mode you care about (docker/gVisor, lima/runc, vps).

The pure logic underneath (classification, shed selection, the blocked-services
ledger logic, status/ledger IO) is covered by the unit tests next to the source
and runs in CI.

## Prerequisites

- A live workspace container with supervisord up and the watchdog running
  (`supervisorctl status memory-watchdog` shows `RUNNING`).
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
   agent* (tier 7) gets shed, revive it with `mngr start <worker> --restart` (a
   shed agent needs `--restart`; a plain `mngr start`/`mngr message` will not
   relaunch it), then message it. Its first turn should carry the injected notice
   that it was stopped for memory and its background tasks were not restarted.
   Verify the ledger gained a `notice_delivered` line for it.

6. **Observe recovery.** supervisord owns liveness, including the watchdog's
   own -- the watchdog supervises nothing itself. Kill the watchdog process to
   confirm supervisord brings it straight back:

   ```bash
   supervisorctl status memory-watchdog   # note the current pid / uptime
   pkill -f "memory-watchdog"
   supervisorctl status memory-watchdog   # RUNNING again within a second or two, new pid
   ```

7. **Crash-loop visibility (reserved -- not yet observable).** supervisord now
   owns restarts (`autorestart` + `startretries`), so nothing writes
   `service_blocked` records today and the banner's paused-service line stays
   empty. There is no drill step for it until a `supervisorctl`-driven poller
   repopulates that signal; see the "Crash-loop visibility (reserved)" section
   of `README.md`.

## Cleanup

Kill any leftover synthetic hog and confirm `is_under_pressure` returns to false.
