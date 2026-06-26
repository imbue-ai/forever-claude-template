The Caretaker now starts each run from a genuinely clean chat, and its welcome behaves correctly over time:

- The first-night welcome now appears only on the very first run (tracked by a persistent `introduced` flag), so it never reappears on later nights even if the Caretaker is recreated.

- On every later run the Caretaker is retired and recreated as a fresh agent, which is the only reliable way to clear its chat (an in-session `/clear` starts a new chat session the workspace UI isn't following). The workspace UI now closes the retired Caretaker's old tab so each run shows a single, fresh, blinking tab.

- The welcome now also asks whether you'd like a first look right away. If you say yes, the Caretaker does its first check immediately on day one instead of waiting until tonight; otherwise it waits for the nightly schedule.
