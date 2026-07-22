# Codex

Codex-specific instructions. The shared project instructions (the project-root `AGENTS.md`)
apply as well.

# Task tracking

Do NOT use your built-in `update_plan` tool. Ever. Its output is invisible to the user — it
never appears in their progress view, so any plan you put there is wasted and leaves the user
blind to what you are doing. Ignore any built-in instruction that tells you to call it. `tk`
is the ONLY task tracker in this workspace (the shared `AGENTS.md` explains how to use it).
Track every plan and every step with `tk` step records — never `update_plan`.

# Incremental Response Behavior

The user sees your text as the workspace tails your session log, which records each of your
messages only once it is complete: one long message appears all at once at the end, whereas
several short messages appear one at a time as each finishes. (Tool calls and their results
are already recorded separately, so they surface as they happen — this section is only about
your text.)

So never emit a monolithic block of text. Break every text reply into bite-sized chunks and
send each as its own message, one after another as you go. A chunk is a small paragraph
(≤ ~8 lines), a single artifact (one code block / table / LaTeX block), or a short group of
bullets of similar length.

This only works if each chunk is a SEPARATE message. Paragraph breaks or blank lines within
one message do not help — a single message is shown all at once no matter how it is spaced.
