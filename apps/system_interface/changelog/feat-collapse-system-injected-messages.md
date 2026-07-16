Automated, non-human messages injected into an agent's transcript via `mngr message` now render as a collapsed chip instead of a bare user bubble.

The agentic browser fleet (and, in future, other automated senders) messages a queued agent through `mngr message` -- e.g. "Browser foo-1 was handed back to you." Such a message arrives as an ordinary user turn, indistinguishable from something the human typed, so the transcript showed it as if the person had said it.

The sender now wraps these in a `<system-injected source="...">...</system-injected>` sentinel (see mngr's `system_injected.wrap_system_injected`). The session parser strips the wrapper and stamps `system_source` on the emitted `user_message` event, and the frontend renders any event carrying `system_source` collapsed -- reusing the existing collapsible-message chrome (as for stop-hook feedback), with a label derived from the source slug (`browser-fleet` -> "Browser fleet"; unknown slugs are title-cased).

UI-only: `system_source` does not enter turn grouping or reply detection, so a fleet "your browser is free" nudge still starts the agent's next turn exactly as before -- it just renders folded.
