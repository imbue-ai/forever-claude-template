# Skill outline fields

The contents of the outline you present at the `outline-approval` gate when the
artifact is a **skill**. (Your operation reference says when this gate fires and
how to write the gate report; this reference only defines what goes inside it.)

The outline contains:

- A kebab-case skill name (naming rules in `spec-summary.md`).
- A one-paragraph description stating what the skill does AND when to use it
  (this becomes the SKILL.md `description` frontmatter).
- Inputs (CLI args if there's a script; prose parameters if agent-driven) and
  outputs (files, stdout, a report the agent hands back).
- A step-by-step flow, each step tagged `[script]` (deterministic),
  `[ai-script]` (model judgement scripted as a model call -- the default for any
  model step), or `[prose]` (user-in-the-loop). Use the re-run test: a step
  whose same prompt/criteria run every time with only the data varying is
  `[ai-script]`, not `[prose]`.
- Prose justification: tag `[prose]` only when the *user* must be in the loop
  while the skill runs; neither a model's judgement nor needing the conversation
  justifies it. Keep genuine prose at the edges, not wedged between scripted
  sections. A pure-prose skill (zero scripts) is valid only when every step is
  genuine executor meta-work.
- Subcommand structure: a subcommand per cleanly-separable step plus a `run all`
  that chains them. Note any step you keep inlined (e.g. it hands the next a
  live handle) and any subflow beyond the natural steps -- those need a specific
  invariant.
- 2-3 evaluation scenarios you plan to hand-craft, plus any edge cases you chose
  not to handle (and why).

When the outline is for a change to an existing skill, it also states the
decision -- update-in-place of `<existing-name>`, or a new sibling named
`<new-name>` -- per `.agents/shared/worker/references/update-vs-create-new.md`.
