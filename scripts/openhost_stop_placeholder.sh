#!/bin/sh
# Stop the boot-time HTTP placeholder (scripts/openhost_boot_placeholder.py)
# and wait for it to release the port, so system_interface can bind. Run by
# the system_interface supervisord program before forward_port/system-interface.
# No-op when the placeholder isn't running (non-OpenHost providers, restarts).
set -u

PID_FILE=/var/run/openhost-boot-placeholder.pid

[ -f "$PID_FILE" ] || exit 0
pid="$(cat "$PID_FILE" 2>/dev/null)" || exit 0
[ -n "$pid" ] || exit 0

# The pidfile persists across supervisord program restarts; only kill if the
# pid is still actually the placeholder.
if ! grep -aq openhost-boot-placeholder "/proc/$pid/cmdline" 2>/dev/null; then
    rm -f "$PID_FILE"
    exit 0
fi

kill "$pid" 2>/dev/null || true
for _ in $(seq 1 50); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
done
rm -f "$PID_FILE"
exit 0
