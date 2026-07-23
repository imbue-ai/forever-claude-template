- The `update-self` flow now bootstraps itself from the version being updated to.
  After the lead resolves the target ref (still with the local resolver), a new
  Step 2a stages that ref's *own* copy of the update-self skill (SKILL.md,
  references, scripts) at a fixed path, and the rest of the pass -- lead *and*
  worker -- runs from the staged copy. So a fix to the conflict-triage,
  validation, or reveal logic that shipped in the target release is applied on the
  way *in*, instead of staying a release behind in the local copy.

- Added a `bootstrap-skill` subcommand to
  `.agents/skills/update-self/scripts/update_self.py`: it `git archive`s the skill
  dir at the resolved ref into a fixed staging dir under `runtime/update-self/`
  (already-fetched objects, no network, no working-tree mutation) and reports
  whether it is byte-identical to the local skill. A ref that predates the skill
  stages the *local* copy at the same path instead, so the staged path always
  holds a runnable flow while the caller cleanly stays on the local flow.

- The staged flow lives at one fixed, literal path --
  `runtime/update-self/skill-at-target/.agents/skills/update-self` -- which the
  lead and worker both address directly. Because it sits under the runtime dir
  synced into the worker's worktree, the worker runs every `update_self.py` call
  from it without any value being carried across shell invocations (Claude's bash
  invocations don't share state, so the earlier env-var approach could silently
  fall back to the stale local copy).
