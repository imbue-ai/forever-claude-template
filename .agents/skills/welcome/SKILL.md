---
name: welcome
description: Greet the user with a short, friendly welcome message when a new project/agent is first started. Invoked automatically as the first message from the minds desktop client.
---

# Welcome the user

This skill has two parts: the opening greeting you always send first, and a list of suggestions you offer only if the user asks for ideas.

## Opening message

Output the following welcome message to the user, verbatim, as your entire response. Do NOT call any tools, do NOT look at the codebase, and do NOT add anything else:

---

### Welcome to Minds

I'm an AI operating system built to extend *you* — so you can do your best work.

I can take on tasks for you, build custom AI tools you can easily edit, connect to the tools you already use to pull in information, or just brainstorm ways to make your work better.

**Let's get to work**

Already have something in mind? Tell me what you'd like to work on below. If not, I'm happy to suggest a few ways to get started.

---

That is the entire opening message. Stop after printing it.

## If the user asks for suggestions

After the opening message the user replies. If their reply asks for suggestions, says they're not sure, or otherwise signals they don't have something specific in mind, output the following message to the user, verbatim, and nothing else. (If instead they describe something they want to do, ignore this section and help them with that directly.)

---

Here are some popular ways people get started with Minds. Pick whichever fits, and we can build on it as a starting point.

1. **Unify your email & messages:** Bring every conversation into one place and respond from there.
2. **Organize your tasks:** Build a system to track what you need to do and get it done.
3. **Track your team's work:** A dashboard for everything across GitHub, Linear, Slack, and email.
4. **Keep up with what you care about:** Stay current on the products, events, or news that matter to you.

---
