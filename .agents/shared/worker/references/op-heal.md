# Operation: heal

The **fix-a-broken-artifact** operation: an existing artifact should have
delivered the correct result but did not, and you apply a minimal root-cause
fix. Load this alongside `harden-artifact.md` (the universal contract) and your
artifact reference (where it lives, how to test it, what not to touch).

A heal has a **single final gate** -- no outline gate. The artifact already
exists; you are not redesigning it, just repairing it.

## Valid report `name:` values

- Gate: `final-artifact` (Stage 6).
- Terminal statuses: `done`, `stuck`.

**System-interface exception.** When the artifact is the system interface, emit
**no `final-artifact` gate**: user approval happens through the lead's pre-merge
live preview, not a worker gate. Run Stages 1-5 as written, then -- once the fix
is implemented and verified per `artifact-system-interface.md` -- report `done`
with a body that summarizes the work so the lead can frame the preview:

```
Fixed the system interface on branch `<branch>`. Ready to preview.
- Change: <one-sentence (root cause + fix)>
- Frontend / backend: <which, and the files touched>
- Tests run: <backend pytest / frontend lint+test / Playwright -- all pass>
- Screenshots reviewed: <pages/states you eyeballed>
```

## Stage 1: Replicate

1. Read the task file: the `## Incident summary` and the `## Anchors` verbatim
   quotes are your primary guide.
2. Locate the incident in the lead's transcript, per
   `.agents/shared/worker/references/transcript-exploration.md`.
3. Read the artifact's current state on disk per your artifact reference.
4. Reproduce the failure. If it depends on external state you cannot recreate,
   construct a minimal synthetic input that exercises the same code path (or,
   for a prose-only artifact, the same branch of the recipe).

## Stage 2: Diagnose the root cause

- Trace the actual runtime code path -- do NOT guess from surface observations.
- Identify the root cause and write it down (one or two sentences) before
  touching anything.
- If you cannot identify a root cause confidently, emit a `stuck` report
  describing what you observed. Don't apply a speculative fix.

## Stage 3: Apply the minimal fix

- Edit only what addresses the root cause, per your artifact reference. Keep the
  fix minimal; don't refactor unrelated code or prose. Fail loudly on unexpected
  input rather than silently swallowing it.
- **A heal is a minimal fix.** If the fix is growing enough to feel like a
  redesign (new flows, expanded contract, new dependencies), stop and emit a
  `stuck` report recommending an update (the change-an-existing-artifact
  operation) instead.

## Stage 4: Re-run 2-3 fresh scenarios

Include the incident itself as one scenario, plus 1-2 that exercise neighbouring
code paths to confirm the fix didn't regress anything. Scenario specifics
(template, fixtures) come from your artifact reference.

## Stage 5: Review gates

Run `/autofix` and the other gates per `harden-artifact.md`; fix what they flag.

## Stage 6: Final gate, then commit and hand off

Write a `type: gate`, `name: final-artifact` report plus "Approve the fix? (yes /
no with notes)", with the body keyed to your artifact:

**Skill:**

```
Fixed `<name>`:
- Root cause: <one-sentence>
- Change: <one-sentence>
- Scenarios run: <list, all pass>
```

**Service:**

```
Fixed service `<name>`:
- Change: <one-sentence (root cause + fix)>
- Routes affected: <list>
- Scenarios / tests run: <list, all pass>
```

Push it and stop. On approval, commit on your branch and emit a `name: done`
terminal report.

## If you cannot fix it

If the root cause is impossible for you to implement, or the right fix would
change the artifact's contract in ways the user should decide on, emit a
`name: stuck` terminal report with a recommendation (e.g. escalate to an update,
or manual investigation).
