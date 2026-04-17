---
name: welcome
description: Greet the user with a short, friendly welcome message when a new project/agent is first started. Invoked automatically as the first message from the minds desktop client.
---

# Welcome the user

Output the following welcome message to the user, verbatim, as your entire response. Do NOT call any tools, do NOT look at the codebase, and do NOT add anything else:

---

Hi! I'm your new project agent, ready to help.

I live in a sandboxed workspace with my own git checkout, tmux session, and background services. You can:

- Chat with me here for quick questions or to kick off work
- Open the agent terminal (link below the chat box) to watch what I'm doing
- Visit the other application tabs (web, system_interface, etc.) that your project exposes

What would you like to work on first?

---

That is the entire welcome message. Stop after printing it.
