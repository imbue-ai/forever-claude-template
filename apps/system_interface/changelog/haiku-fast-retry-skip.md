Fast retry: the time-gated "Skip" control now answers the same question faster
instead of just aborting the turn.

- When a chat turn runs long, the activity strip surfaces a "Skip" control (after
  20s, alongside an elapsed timer). Clicking it used to just interrupt the turn
  and hand control back. It now performs a **fast retry**: it cancels the slow
  in-flight turn and immediately re-asks the *same* user message, answered by
  Haiku with minimal thinking -- so the user gets a fast, shallow answer instead
  of waiting out the slow deep one (mirrors the Dia browser's "skip the
  reasoning, answer again" behavior).

- Mechanics (frontend orchestration in `ActivityIndicator.ts`, reusing
  `interruptAgent` + `sendMessage`): interrupt the agent (the only cancel
  primitive -- it restarts the agent to an idle state, preserving conversation
  history but resetting the model to the opus default), then send `/model haiku`
  and `/effort low`, then re-send the last genuine user message. The re-asked
  prompt is found by walking the transcript from the tail and skipping control
  chatter, so a prior fast retry's own `/model` / `/effort` messages are never
  mistaken for the question. If there is no prior user message to re-ask, the
  control still cancels the turn (a plain interrupt).

- The model-switch chatter is hidden from the chat: `/model` and `/effort`
  slash-command invocations and their `<local-command-stdout>` echoes are now
  classified as hidden user messages (`message-classification.ts`), so the
  transcript shows only the re-asked question and its fast answer -- not the
  control commands. The underlying transcript events are unchanged; only the
  rendering hides them.

- The model switch is intentionally **not** restored afterwards. Restoring would
  require sending `/model opus` / `/effort high` to a busy agent, where message
  delivery is unreliable (queued sends to a mid-turn agent can be lost), so the
  agent stays on the fast model after a fast retry until the next interrupt
  (which resets it) or a manual `/model` change. A follow-up can add reliable
  restore if desired.
