New `browser` web service: an on-demand live Chromium you can watch and drive from a tab, optionally driven by an AI agent.

A headless Chromium is launched inside the compute and streamed to the tab over the Chrome DevTools Protocol (`Page.startScreencast` → JPEG frames over a WebSocket); your clicks, typing, and scrolling are injected over CDP too, so interaction is low-latency. A reimplemented tab strip + URL bar mirror the browser's real tabs and follow whatever page is active — including when the agent opens or switches tabs. The URL bar reflects the page's actual address (it updates on real navigation, never optimistically).

A side panel adds a ChatGPT-style chat that hands the same browser to a browser-use agent (model `claude-sonnet-4-6`, stock prompt). While the agent runs, the status box shows "Agent has control" and your browser input — including the tab strip and URL bar — is locked. A message you type while it's running is queued (one pending, cancelable) and runs after the current task; a "Take control" button stops the agent and returns control to you (send a new message to continue — there is no auto-resume). Agent thinking and actions stream into the chat as separate collapsible blocks.

Concurrent sessions are capped (default 3, `BROWSER_MAX_SESSIONS`) to keep a small compute from running out of memory, and the stream auto-reconnects if its WebSocket drops.
