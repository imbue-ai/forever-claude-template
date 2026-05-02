# Verify flow (worker)

Follow this flow when your task file carries `FLOW: verify`. The main agent
and the user already discussed and committed the change before this worker
was launched. Your job is verification, not design -- there is no Gate 1.
Only Gate 2 (final-artifact) applies.

## Stage 1: Read the committed change

1. Read the task file's `## Committed change` section: branch, commit range,
   summary, and design rationale.
2. Read `runtime/update/<target>/commit.diff` and
   `runtime/update/<target>/commit.log` (pushed by the lead) to see the
   actual diff text.
3. Read the target skill's current `SKILL.md` and any scripts under
   `scripts/` on disk. Because `mngr create` branched the worker off the
   lead's branch, the committed change is already present in your worktree
   -- this is the post-change state, and it is what scenarios will exercise.

If the diff references `references/*.md` files or other supporting files,
read those too.

## Stage 2: Verify the change against the rationale

Cross-check the committed change against the design rationale stated in the
task file. You are looking for:

- **Fidelity:** does the diff actually implement what the rationale
  describes? Flag any step from the rationale that the diff does not
  contain, and any major change in the diff that the rationale does not
  explain.
- **Consistency:** does the edited SKILL.md stay coherent? References to
  removed content, orphaned sections, or broken links are verification
  findings, not judgement calls about the design.
- **Backward compatibility:** if the rationale claims backward
  compatibility, do existing callers still work? (E.g. old task-file shapes
  still route correctly.)

Do not redesign. If the diff diverges from the rationale in a non-trivial
way, note the divergence as a Gate 2 finding for the user to adjudicate.

## Stage 3: Validate skill layout

Run `validate_skill.py` per `../SKILL.md`. It must print `ok` before moving
on.

## Stage 4: Run 2-3 scenarios

Per `../SKILL.md`. At least one scenario must exercise the changed path (so
the verification is real, not just "the file parses"). Others should
exercise neighbouring paths the rationale claims are unaffected -- that is
how you catch regressions.

Scenarios are walk-throughs against the post-change skill files on disk.
Record each in your transcript using the template from
`.agents/shared/references/spec-summary.md`.

## Stage 5: Code review

Run `/autofix` on the branch. Fix anything the reviewer flags; those fixes
become follow-up commits on `mngr/update-<target>`.

## Stage 6: Gate 2 -- final artifact

Write a report with `type: gate`, `name: final-artifact`, and body:

```
Verified live update to `<name>`:
- Commit range: <range>
- Diff fidelity: <matches rationale | diverges: ...>
- Consistency check: <clean | findings: ...>
- Scenarios run: <list, all pass>
- Autofix: <clean | fixes committed: ...>

Approve and save? (yes / no with notes)
```

Push it and stop, per the reporting procedure in `../SKILL.md`.

## Stage 7: Hand off

On approval, emit a `name: done` terminal report (body shape per
`.agents/shared/references/worker-reporting.md`; this is the verify variant).
If verification produced no worker commits of its own (clean run, no
`/autofix` fixes), that is fine -- the merge still brings the original live
commit forward, and the `done` status tells the lead to merge.
