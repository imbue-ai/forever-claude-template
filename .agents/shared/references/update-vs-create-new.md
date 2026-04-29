# Update-in-place vs. create-new-skill

When updating a skill because additional repeatable work was done by hand,
decide: extend the existing skill, or split off a new sibling skill?

**Default to update-in-place.** Only split when the extra work would plausibly
be useful on its own -- in a context that does not involve the existing skill.

- **Update-in-place** when the gap is a natural extension of the existing
  skill (extra flag, new output format, edge case the skill did not cover, an
  additional judgement step in the same flow), OR when the gap is only useful
  in the context of this skill's process (you cannot concretely imagine
  invoking it standalone). The skill's identity and primary purpose stay the
  same.
- **Create-new-skill** when the gap is orthogonal AND has a concrete
  standalone use case -- another agent in another flow would reasonably want
  to invoke it without the existing skill. Pick a fresh kebab-case name; the
  old skill stays untouched. Don't decompose proactively for hypothetical
  reuse.

Script vs. prose is orthogonal to this decision. An update-in-place can land
as a new script step, a new prose step, or both. Same for a create-new-skill.

If update-in-place would double the size of the original SKILL.md or blur its
one-line description, that is a signal to split (combined with the
standalone-use-case check above).

If the extra work was **one-off creative or exploratory** with no repeatable
pattern, it is NOT an update candidate -- it stays with the main agent.
Judgement work with a repeatable recipe IS a candidate; it becomes a prose
step in SKILL.md.

In the `verify` flow the decision has already been made by the committed
change; the worker's job is to verify, not to re-litigate.
