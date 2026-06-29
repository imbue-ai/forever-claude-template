# When to crystallize

You are deciding whether the just-finished turn is worth crystallizing into a
reusable skill via `crystallize-artifact` (artifact = skill) -- and the
**default is to ask the user**, not to decide silently.

## The rule

1. Try to name a concrete skill shape: one sentence of the form "a skill
   that does X given (Y) produces Z." You do NOT have to be confident
   it's a great skill. A plausible shape is enough.
2. If you can name one: send a one-line question to the user via
   `send-user-message` proposing that shape, and let the user decide.
3. If you genuinely cannot name any shape after trying: decline.
   "Genuinely cannot" means the work had no stable structure across
   hypothetical re-runs -- not merely that you're skeptical the skill
   would be useful.

## The re-run test

Mentally simulate the re-run. If the user asked you to do this same task
again with different inputs, walk through the work step by step and
classify each step:

- **Identical**: same endpoint called, same URL or query pattern, same
  parsing/post-processing logic. Only the data varies between runs.
- **Structurally same**: different values plugged into the same query
  shape -- still scriptable, just parameterized.
- **New judgement**: qualitative evaluation, ranking by fuzzy criteria,
  deciding "is this result trustworthy," "does this belong in the output."

The identical and structurally-same parts are the skill's deterministic
substructure. The judgement parts are ALSO part of the skill -- scripted as
`[ai-script]` model calls so the flow runs headless. A step stays `[prose]`
only when the skill needs the *user* in the loop while it runs.

If much of the re-run would be literally the same work, you have a
candidate. Diff the original run against the hypothetical re-run; what's
shared is the skill's process.

## A skill captures a process, not just a script

A skill is a SKILL.md recipe ("do X, then Y, then Z") plus supporting
scripts, references, or assets. Each step is `[script]`, `[ai-script]`, or
`[prose]`. Model-judgement
steps are scripted as `[ai-script]` calls by default, not parked in prose, so
a process like fetch (`[script]`) -> natural-language filter (`[ai-script]`)
-> dedupe and format (`[script]`) runs fully headless.

So do not require end-to-end *determinism* before crystallizing -- what
matters is whether the *process* is stable across runs, not whether every
step is deterministic.

## Reasoning traps

Before you decline, check whether your reasoning matches any of these:

- **"This was one-off."** This turn may have involved one-off work (e.g. identifying data sources); that does not mean that the whole task was one-off. Consider the output that you generated - is it possible the user may want this output regenerated based on updated data or using different parameter values?
- **"The data sources change too fast."** Fragility is manageable via
  `heal-artifact` when the skill is used often. You can flag to the user if you
  think this is a serious concern, but it shouldn't by itself be a reason not to
  crystallize.
- **"The hard part was judgement."** Setup judgement (which sites, which
  filters, which approach) is often a one-time cost paid during the first
  run; the crystallized skill captures the *post-setup* process. Ongoing
  judgement steps are scripted as `[ai-script]` calls, not a reason to
  decline.
- **"No sub-process is clean enough."** You don't need the whole turn
  to be crystallizable. A stable inner loop (fetch-dedupe-rank,
  filter-and-diff, lint-and-report) is sufficient. Extract just that.
- **"I should let the user ask if they want it."** Users don't always
  know this capability exists, or won't think to ask. Surfacing the
  option IS the affordance.

## When to genuinely decline

- Pure creative work (writing, design exploration).
- Single-shot debugging where the fix is already in the diff and the
  root cause was unique to this codebase/moment.
- Research answering a one-time question with no follow-up structure
  (e.g. "what's the difference between X and Y?").
- Mixed-bag turns where different parts had nothing in common.
- Work with no stable structure across hypothetical re-runs -- each
  re-run would require entirely different steps, not just different
  data.
- If the user came to you with the task of planning out and implementing a complex code-based product already -- there's no need to muddy the water of the implementation by adding skills into the mix or duplicating work; just implement what the user is asking you to do.

## Skill-shape sanity checks

A good crystallization candidate usually has:

- Clear inputs (even if many): filters, target, credentials, constraints.
- A clear output shape: a list, a diff, a report, a pass/fail.
- A re-run semantics story: "how does this behave on call #2?"
- A stable process across runs, even if some steps are judgement.

If three of four are present, name the shape and ask.