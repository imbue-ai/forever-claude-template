---
name: suggest-starting-points
description: Show the user a short menu of concrete ways to get started -- consolidating their messages, managing their tasks, building a monitoring dashboard, or research-and-report -- as clickable choice cards. Use when the user asks you to suggest things to work on, asks what you can do for them, or picks "Suggest a few things" from the welcome.
---

# Suggest starting points

Reply with one short framing line, then the choices block below, verbatim. Do NOT
call any tools and do NOT add anything after the block:

---

Here are some popular ways people get started with Minds. Pick whichever fits, and we can build on it as a starting point.

```minds-choices
[
  {"title": "Make a custom email & messaging hub", "subtitle": "Organize all your messages and emails in one place, make it easy to respond.", "prefill": "Help me consolidate all my messages and email in one place so it's easy to respond."},
  {"title": "Make a custom view of your tasks", "subtitle": "Build a system to organize your tasks and help you get them done.", "prefill": "Help me build a system to organize my tasks and get them done."},
  {"title": "Make a custom team view", "subtitle": "Make a dashboard for anything you want to track and take action on — things in GitHub, Linear, Slack or email.", "prefill": "Help me build a dashboard to keep an eye on things from various services and make it easy to take actions."},
  {"title": "Make a custom report", "subtitle": "Stay up to date on the products, events, or news that you care about.", "prefill": "Help me stay up to date on the industries, events, and news that I care about."}
]
```

---

The ` ```minds-choices ` block renders as four clickable cards; clicking one
sends that message for the user. Output it exactly as written. Stop after the
block.
