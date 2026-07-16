- The `update-self` flow now bootstraps itself from the version being updated to.
  After the lead resolves the target ref (still with the local resolver), a new
  Step 2a extracts that ref's *own* copy of the update-self skill (SKILL.md,
  references, scripts) and, if it differs from the local copy, the rest of the
  pass -- lead *and* worker -- runs from the extracted copy. So a fix to the
  conflict-triage, validation, or reveal logic that shipped in the target release
  is applied on the way *in*, instead of staying a release behind in the local
  copy.

- Added a `bootstrap-skill` subcommand to
  `.agents/skills/update-self/scripts/update_self.py`: it `git archive`s the
  skill dir at the resolved ref into a staging dir under `runtime/update-self/`
  (already-fetched objects, no network, no working-tree mutation) and reports the
  staged path plus whether it is byte-identical to the local skill. A ref that
  predates the skill reports no staged copy, so the caller cleanly falls back to
  the local flow.

- The lead passes the resolved flow location to the worker through a new
  `update_self_skill_dir` task-frontmatter field (surfaced to the worker as
  `$UPDATE_SELF_SKILL_DIR` by the existing `parse_task_frontmatter.py` eval). The
  worker guide now runs every `update_self.py` call from
  `$UPDATE_SELF_SKILL_DIR/scripts/`, with a fallback to the local path so an older
  task file that omits the field still works.
