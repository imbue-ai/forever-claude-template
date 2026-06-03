# Exploring the lead's transcript

Your task body describes something that happened in the lead's session --
an incident, a turn of work, or a design conversation -- and anchors it
with verbatim quotes under `## Anchors`. Use those quotes to find the real
turns.

`$LEAD_AGENT` comes from the task frontmatter (parsed per
`worker-reporting.md`). To explore:

1. Run `mngr transcript $LEAD_AGENT --role user --role assistant` to read
   the conversation with tool-call noise stripped, and search it for the
   anchor quotes.
2. Once you have located the relevant region, re-read it in full detail
   (default format, scoped with `--tail N`) to see exactly which tools
   ran, with what inputs, and why.

The turn that dispatched you is the *most recent* turn in the lead's
transcript. What your task body describes is always in *earlier* turns --
never treat the dispatch turn itself as the subject.

Do NOT re-execute destructive operations you find in the transcript.
Reading it is enough.
