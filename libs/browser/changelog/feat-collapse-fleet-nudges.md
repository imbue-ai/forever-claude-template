The fleet's agent nudges (browser handed back, browser gone) are now tagged as automated so the transcript UI collapses them instead of rendering a bare user bubble.

`BrowserSession._message_agent` -- the path that messages a queued agent via `mngr message` when its browser frees up or disappears -- now passes `--system-source browser-fleet`. `mngr message` wraps the text in the `<system-injected>` sentinel that the `system_interface` transcript parser recognises, strips, and renders folded. No behavior change: the agent still resumes its turn on receipt exactly as before.
