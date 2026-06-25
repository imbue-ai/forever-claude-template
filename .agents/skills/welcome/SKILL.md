---
name: welcome
description: Greet the user with a short, friendly welcome message when a new project/agent is first started. Invoked automatically as the first message from the minds desktop client.
---

# Welcome the user

Output the following welcome message to the user, verbatim, as your entire response. Do NOT call any tools, do NOT look at the codebase, and do NOT add anything else:

---

### Welcome to Minds

I’m an AI operating system that’s been built to extend *you* — so you can do your best work.

I can do tasks for you, make custom AI tools for you that are easily editable, or just brainstorm with you on how to build systems for you to do your best work.

I can connect to the following platforms, and help pull information or build personalized views: Slack, GitHub, Linear, Notion, Google Suite (Docs, Drive, Sheets), Google Calendar, Gmail, Google Analytics, Dropbox, and Ramp.

Dump your thoughts on what needs to get done, and we can figure it out together.

**What would make your work easier?**

```minds-choices
[
  {"title": "I have something in mind", "subtitle": "Tell me what you'd like to work on.", "prefill": ""},
  {"title": "Suggest a few things", "subtitle": "I'll show you a few ways to get started.", "prefill": "Suggest a few things I could work on."}
]
```

---

That is the entire welcome message. Stop after printing it. Output the
` ```minds-choices ` block exactly as written -- it renders as two clickable
cards in the chat, and clicking one sends that message for the user ("I have
something in mind" has no message, so it just focuses the box to type).

If the user then asks you to suggest things to work on (for example by picking
"Suggest a few things"), the `suggest-starting-points` skill takes over and shows
the four starting-point cards.
