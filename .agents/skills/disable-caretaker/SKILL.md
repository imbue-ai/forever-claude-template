---
name: disable-caretaker
description: Switch off the weekly Caretaker. Use when the user asks to turn off, pause, or get rid of the Caretaker.
---

# Disable the Caretaker

To switch the Caretaker off:

    rm runtime/caretaker/enabled

That is the whole switch: the weekly check stops immediately and the agent is
never woken again. Its notes and permissions file stay put, so re-enabling
later (the enable-caretaker skill) picks up where it left off.

If the user also wants its state gone, additionally remove
`runtime/caretaker/` -- but confirm first, since that deletes the Caretaker's
run history and their recorded permissions.
