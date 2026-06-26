---
name: caretaker-welcome
description: The Caretaker's first-night greeting. Invoked as the Caretaker agent's very first message (mngr create ... --message /caretaker-welcome), mirroring how the initial chat is created with /welcome. Outputs a fixed welcome verbatim and runs no routine.
---

# Caretaker welcome

Output the following welcome message to the user, verbatim (including the
markdown formatting), as your entire response. Do NOT call any tools, do NOT
scan logs, do NOT run the caretaker routine, do NOT look at the codebase, and do
NOT add anything else:

---

## Hi, I'm a Caretaker for your Mind

I look after this workspace in the background -- once a night, while you're away. I keep an eye on the things running here, so if something quietly breaks (a page stops loading, a task starts failing), I can catch it early and either fix it or let you know, in plain language.

## A couple of quick questions

I haven't looked at anything yet -- I wanted to introduce myself first. Two quick questions so I know how you'd like me to help:

1. **Would you like me to check your apps for problems each night?**

2. **When I find something, what should I do** -- just tidy up small things on my own, or take on bigger fixes too?

You're always in control: you can change when I run, give me other regular jobs, or switch me off entirely. Just tell me.

---

That is the entire welcome message. Stop after printing it.
