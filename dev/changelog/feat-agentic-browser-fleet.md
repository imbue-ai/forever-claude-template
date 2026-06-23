Added the `agentic-browser-fleet` skill, which teaches the agent to drive the
per-workspace browser fleet DIRECTLY via the `agentic-browser-fleet` CLI: it runs
`state <id>` to see the page as a numbered list of clickable elements, then
`open`/`click`/`input`/`scroll`/`keys`/`screenshot`/`tab` to act on it, doing its
own reasoning (no API key) one command at a time. It documents choosing a browser
(`ls [--include-tabs]`), the re-query-the-page-after-each-change discipline, the
ownership rules (agents never preempt each other; a human "Take control" makes the
agent's next command a clean "lost control", and it resumes only when told to via
`--reclaim`), the exit codes, and how to anchor a sub-agent's browser pane next to
the parent's chat (`BROWSER_FLEET_ANCHOR`).

The `scripts/layout.py` agent helper can now address a specific browser session
as a pane ref (`service:browser?session=<id>`).
