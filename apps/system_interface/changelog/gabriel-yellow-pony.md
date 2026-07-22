Added a model picker and fast-mode toggle to the chat composer.

The composer toolbar (next to the attach and send buttons) now has a compact model picker (Fable 5 / Opus 4.8 / Sonnet 5 / Haiku 4.5, defaulting to Opus with its 1M-token context window) and, beside it, an icon fast-mode toggle. The toggle only appears for models that support fast mode (Opus), lights up while fast mode is on, and its tooltip reflects whether clicking will enable or disable it. The picker opens a small menu, anchored to the right of the composer, listing the models under a "Model" heading.

Picking a model or flipping the toggle applies to the running agent immediately -- it sends the agent a `/model` or `/fast` command, which Claude Code applies live and persists as the agent's default. The picker reads the agent's current selection from its Claude Code settings, so it always shows what the agent is actually using.

The `/model` and `/fast` commands (and Claude Code's confirmation lines) are hidden from the chat transcript so the picker and toggle don't clutter the conversation. They also no longer make the activity indicator briefly read "Thinking..." -- the indicator now ignores these injected commands (and their confirmations) when deciding whether the agent has been handed work, so changing the model or fast mode leaves an idle agent shown as idle.

The model picker is now a plain, bare text button rather than a boxed control: it drops the filled background and border, uses the interface's regular sans-serif font (in place of the monospace one), and shows a light hover highlight, so it reads as a subtle control that matches the icon buttons beside it. The dropdown's options use the same regular font.
