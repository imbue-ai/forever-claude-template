---
name: enable-caretaker
description: Turn on the weekly Caretaker, a BETA feature that is off by default. Use ONLY when the user explicitly asks about the Caretaker or about enabling it -- never suggest, mention, or enable it proactively.
---

# Enable the Caretaker (BETA)

The Caretaker is a **beta** feature, off by default: a background agent that
checks this workspace about once a week -- crashed services, fresh errors in
the logs, a filling disk, memory trouble -- and opens a chat tab only when
there is something to say.

Before enabling it, make sure the user genuinely wants it: say plainly that
it is a beta feature and they should not expect it to be polished or optimal,
and get their explicit confirmation. Only proceed on a clear yes.

To enable:

    mkdir -p runtime/caretaker && touch runtime/caretaker/enabled
    rm -f /var/lib/minds/daily-stamps/caretaker

It introduces itself shortly afterwards (within a minute or two during the
day; at about 3 AM if enabled in the small hours), then checks weekly.

To switch it off again, use the disable-caretaker skill
(`rm runtime/caretaker/enabled`); its notes and permissions stay put for a
later re-enable.
