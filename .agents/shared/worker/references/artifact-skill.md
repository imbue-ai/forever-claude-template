# Artifact: skill

A reusable skill under `.agents/skills/<name>/` -- a SKILL.md process recipe plus
the scripts its steps call. `.agents/shared/references/spec-summary.md` is the
authority on the agentskills.io spec: directory layout, frontmatter, the
`[script]` / `[ai-script]` / `[prose]` step kinds, `run.py` packaging,
validation, and the scenario template. This reference adds only what that spec
cheat-sheet and the universal `harden-artifact.md` contract don't already cover.

**Design judgement.** Reliability is the floor; simplicity is the target. Default
to a subcommand per cleanly-separable step plus a `run all` that chains them; add
surface beyond that only when a specific invariant demands it. Split into a
*separate* skill only when the components are likely to be used independently.

## Where a skill's behavior lives

A skill's behavior is split between its scripts (`[script]` / `[ai-script]`, in
`scripts/`) and its SKILL.md prose, so a change -- or a fix -- may touch either or
both. When a wrong behavior traces to an ambiguous or incorrect prose
instruction, the edit is a SKILL.md edit even if the skill has scripts; a
pure-prose skill (no scripts) has all of its behavior in SKILL.md.

- A crystallized skill is marked `metadata.crystallized: true`.
- Keep SKILL.md under ~500 lines; split long content into `references/`.
- **Cross-section alignment sweep** (after any localized edit): update the
  frontmatter `description`, the H1/opening prose, any principle bullets, section
  headings, cross-references, and `## Conventions` / `## Gotchas` -- every place
  that names or summarizes the changed material.

## Testing a skill

- Validate with `uv run .agents/shared/scripts/validate_skill.py
  .agents/skills/<name>` -- it must print `ok` (see `spec-summary.md` for what it
  checks).
- Hand-craft and run 2-3 scenarios (template in `spec-summary.md`); they are
  **ephemeral** -- run them in your transcript, never saved as files. For a
  `[script]` / `[ai-script]` step, invoke `scripts/run.py` on real input and
  inspect the output (an `[ai-script]` step makes a real Claude call -- run it on
  a small input to note cost). For a `[prose]` step, walk the SKILL.md
  instructions as the executing agent.
- The universal fixture-test rule (`harden-artifact.md`), for a skill: save 1-3
  samples under `.agents/skills/<name>/tests/fixtures/` and add a
  `scripts/<name>_test.py` that feeds each through the parser and asserts the
  exact output shape.

## Data capture

Beyond the universal preserve-and-surface rule (`harden-artifact.md`), persist
each record under `runtime/<name>/`, capture *all reasonable fields per record*
in the calls you already make (not just the fields the original turn displayed),
and treat pagination as normal when the ask requires it -- but do NOT make extra
un-asked-for API calls just to gather more data.

## Built-in skills

Some skills are built-ins synced from the upstream template (`parent.toml`).
Editing one creates local drift to reconcile later; treat such an edit as a
change to shared infrastructure, not a private one.
