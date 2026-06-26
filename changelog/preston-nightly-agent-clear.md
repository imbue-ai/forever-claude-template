The nightly **Caretaker** now starts each night from a genuinely clean chat.

Previously the Caretaker was asked to clear its own conversation, which never actually worked (an agent writing "/clear" only emits text; the command never fires). Now mngr clears the chat for it: when the scheduler re-wakes an existing Caretaker, it first wipes the prior conversation and then sends the run trigger, so each night's chat begins fresh.

On the very first night the Caretaker appears, the first thing you see is its clean welcome message introducing itself -- no leftover or routine narration before it.
