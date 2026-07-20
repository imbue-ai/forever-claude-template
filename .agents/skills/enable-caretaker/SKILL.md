---
name: enable-caretaker
description: Turn on the weekly Caretaker, a BETA feature that is off by default. Use ONLY when the user explicitly asks about the Caretaker or about enabling it -- never suggest, mention, or enable it proactively.
---

# Enable the Caretaker (BETA)

The Caretaker is a **beta** feature, off by default: a background agent that
checks this workspace about once a week -- crashed services, fresh errors in
the logs, a filling disk, memory trouble -- and opens a chat tab only when
there is something to say. Off by default means literally nothing runs: no
cron entry exists until this skill creates it.

Before enabling it, make sure the user genuinely wants it: say plainly that
it is a beta feature and they should not expect it to be polished or optimal,
and get their explicit confirmation. Only proceed on a clear yes.

To enable, write the Caretaker's schedule entry and clear any stale stamp:

    printf '%s\n' '* * * * *   root   /mngr/code/scripts/with_agent_env.sh /mngr/code/scripts/run_daily_job.sh caretaker 3 --interval-days 7 bash /mngr/code/scripts/caretaker_check.sh >> /var/log/supervisor/caretaker-job.log 2>&1' > /etc/cron.d/minds-caretaker
    chmod 0644 /etc/cron.d/minds-caretaker
    rm -f /var/lib/minds/daily-stamps/caretaker

This is the standard daily-job pattern from the manage-scheduled-tasks skill:
the every-minute tick is `run_daily_job.sh`'s due-checker (weekly, due hour
3, catch-up after downtime), and `caretaker_check.sh` is the deterministic
check that wakes the agent only when it finds something. Cron picks the new
file up within a minute, so the Caretaker introduces itself shortly
afterwards (within a minute or two during the day; at about 3 AM if enabled
in the small hours), then checks weekly.

To switch it off again, use the disable-caretaker skill
(`rm -f /etc/cron.d/minds-caretaker`); its notes and permissions stay put for
a later re-enable.
