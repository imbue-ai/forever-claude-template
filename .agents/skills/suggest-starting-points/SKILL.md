---
name: suggest-starting-points
description: Show the user a short menu of concrete ways to get started -- consolidating their messages, managing their tasks, building a monitoring dashboard, or research-and-report -- as clickable choice cards. Use when the user asks you to suggest things to work on, asks what you can do for them, or picks "Suggest a few things" from the welcome.
---

# Suggest starting points

Reply with one short framing line, then the choices block below, verbatim. Do NOT
call any tools and do NOT add anything after the block:

---

Here are a few ways I can help you get started -- pick whichever fits, or just
tell me what you have in mind:

```minds-choices
[
  {"title": "Consolidate your messages", "subtitle": "Organize all your messages and email in one place, and make it easy to respond.", "prefill": "Help me consolidate all my messages and email in one place so it's easy to respond."},
  {"title": "Manage your tasks", "subtitle": "Build a system to organize your tasks and help you get them done.", "prefill": "Help me build a system to organize my tasks and get them done."},
  {"title": "Keep an eye on things", "subtitle": "Build a dashboard for anything you want to track -- Slack, email, GitHub, Linear -- and make it easy to act on.", "prefill": "Help me build a dashboard to keep an eye on things like Slack, email, GitHub, and Linear, and make it easy to take actions."},
  {"title": "Research and report", "subtitle": "Stay up to date on the industries, events, or news you care about.", "prefill": "Help me stay up to date on the industries, events, and news I care about."}
]
```

---

The ` ```minds-choices ` block renders as four clickable cards; clicking one
fills in the user's message for them. Output it exactly as written. Stop after the
block.
