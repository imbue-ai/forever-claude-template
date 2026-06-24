Added the `agentic-browser-fleet` skill, which teaches the agent to drive the
per-workspace browser fleet DIRECTLY via the `agentic-browser-fleet` CLI: it runs
`state <id>` to see the page as a numbered list of clickable elements, then
`open`/`click`/`input`/`scroll`/`keys`/`screenshot`/`tab` to act on it, doing its
own reasoning (no API key) one command at a time. It documents choosing a browser
(`ls [--include-tabs]`), the re-query-the-page-after-each-change discipline, the
ownership rules (agents never preempt each other; a human "Take control" makes the
agent's next command a clean "lost control", and it resumes only when told to via
`--reclaim`), and the exit codes. It now also tells the agent to `release <id>`
the moment it is done with a browser -- handing control straight back to the human
instead of leaving a grey "Agent has control" overlay up until the ~90s idle
timeout. And it clarifies that browser work belongs to the user-facing agent the
human is watching: a `launch-task` sub-agent runs in a separate, isolated container
with no access to this workspace's browser fleet or its live panes, so the agent
drives the browser itself in this chat rather than delegating it to a background
sub-agent.

The skill now also states the fleet cap (5 browsers; `new` past it returns "Too
many open browsers", so release/close one first) and the "another browser vs.
wait" rule: when *another agent* holds a browser, take a different one (their task
lives there); when a *human* takes *your* browser mid-task, your work is on it, so
wait and resume that same one.

The `scripts/layout.py` agent helper can now address a specific browser session
as a pane ref (`service:browser?session=<id>`).
