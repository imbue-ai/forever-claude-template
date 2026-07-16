Visual refresh of the system interface UI.

- The chat transcript area is now pure white, with the message footer unified
  into the same white surface.

- All inline SVG icons are consolidated into a single shared module
  (`frontend/src/views/icons.ts`) instead of being redefined across individual
  views (login modal, message input, progress block, permission card, etc.).

- General visual refresh pass across the Claude login modal, message input,
  progress blocks, permission cards, and lightbox.

- Timeline and tab-strip polish: refined the run timeline (masked the connector
  line above ungrouped runs, bolder titles) and user message bubbles, plus
  tab-strip tweaks (bleed and a centered "+" button).
