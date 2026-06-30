The "+" tab menu's "New URL" item is replaced with "New browser", which opens a live streamed Chromium session (see the `browser` service) in a new tab.

The item is enabled only when an Anthropic API key is resolvable inside the compute (required by the browser-use agent); otherwise it is greyed out with a message explaining which workspace providers supply a key. The key status is re-checked each time the menu opens, so a key added after boot enables the item without a reload.
