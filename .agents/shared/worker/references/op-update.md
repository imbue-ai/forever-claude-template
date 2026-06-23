# Operation: update

The **change-an-existing-artifact** operation: extend, refactor, or verify an
artifact that already exists. Load this alongside `harden-artifact.md` (the
universal contract) and your artifact reference (where it lives, how to test it,
what not to touch).

The artifact already exists on disk, so there is nothing to reconstruct. The one
axis of variation is a **design-gate toggle** keyed on whether the change is
already committed:

- **Committed** -- the lead and the user discussed the change live and it is
  already committed on the branch (you branched off it, so it is present in your
  worktree). The design was approved organically in chat, so **skip the design
  gate**; read the committed change as ground truth, verify it, and present the
  final gate.
- **Emergent** -- a skill ran but additional *repeatable* work had to be done by
  hand, with no design conversation. **Run the design gate first**: reconstruct
  the incident from the lead's transcript, propose a design, and get it approved
  before implementing.

Read the toggle from the task file's `## Change origin` marker (`ORIGIN:
committed` or `ORIGIN: emergent`). The marker is required -- fail loudly if
absent or unrecognized.

## Valid report `name:` values

- Gates: `outline-approval` (emergent only -- the design gate), `final-artifact`
  (both).
- Terminal statuses: `done`, `stuck`, `no-update-needed`.

**System-interface exception.** When the artifact is the system interface, there
is no `## Change origin` toggle and **no gate report at all**: the change is
handed to you as a plain brief, and user approval happens through the lead's
pre-merge live preview, not a worker gate. Implement the brief, verify it per
`artifact-system-interface.md`, then report `done` (or a mid-flight `question`,
or `stuck`) with a body that summarizes the work so the lead can frame the
preview:

```
Updated the system interface on branch `<branch>`. Ready to preview.
- Change: <one-sentence>
- Frontend / backend: <which, and the files touched>
- Tests run: <backend pytest / frontend lint+test / Playwright -- all pass>
- Screenshots reviewed: <pages/states you eyeballed>
```

The committed/emergent paths and gates below apply only to skill and service
artifacts.

## Emergent path

### Stage 1: Replicate

1. Read the task file: the `## Incident summary` and the `## Anchors` verbatim
   quotes are your primary guide.
2. Locate the incident and the manual follow-up in the lead's transcript, per
   `.agents/shared/worker/references/transcript-exploration.md`.
3. Read the artifact's current state on disk per your artifact reference.

### Stage 2: Design and Gate 1

For a **skill** artifact, first decide update-in-place vs. split-a-new-sibling
per `.agents/shared/worker/references/update-vs-create-new.md` (default to in-place;
only split when the gap has a concrete standalone use case). Other artifacts
update in place.

Propose an outline. For a **skill**, the outline contents are defined in
`.agents/shared/worker/references/skill-outline-fields.md`; add the update decision
(update-in-place vs. new sibling). For a **service**, the
outline is the decision, what changes, and the routes/scenarios affected. Write a
`type: gate`, `name: outline-approval` report with the outline plus "Approve this
outline? (yes / no with notes)". Push it and stop. Wait for an explicit yes
before coding.

### Stage 3: Implement

Edit the artifact per your artifact reference, preserving the existing contract
for current callers unless the outline calls for a breaking change. After the
localized edit, sweep the rest of the artifact for any cross-reference,
summary, or description that names the changed material and update it too --
treat the sweep as part of the edit, not a follow-up.

## Committed path

### Stage 1: Read the committed change

1. Read the task file's `## Committed change` section: branch, commit range,
   summary, and design rationale.
2. Read the pushed `commit.diff` / `commit.log` (staged alongside the task file)
   to see the actual diff, and read the artifact's post-change state on disk.
3. If the rationale alludes to conversational context the diff does not explain,
   locate those turns in the lead's transcript.

### Stage 2: Verify against the rationale

Cross-check the committed change against the stated design rationale: does the
diff implement what the rationale describes (fidelity)? Does the edited artifact
stay coherent -- no orphaned references, broken links, or cross-reference drift
(consistency)? If the rationale claims backward compatibility, do existing
callers still work? Do not redesign; note any divergence as a final-gate finding
for the user to adjudicate.

## Both paths

### Validate and run scenarios

Validate the artifact per your artifact reference, then run 2-3 scenarios. At
least one must exercise the changed path (the absorbed incident in the emergent
path; the changed path in the committed path); others exercise neighbouring or
edge paths to catch regressions.

### Review gates

Run `/autofix` and the other gates per `harden-artifact.md`. In the committed
path, any fixes become follow-up commits on your branch.

### Final gate, then hand off

Write a `type: gate`, `name: final-artifact` report plus "Approve and save? (yes
/ no with notes)", with the body keyed to your artifact:

**Skill:**

```
<Updated | Created> `<name>`:
- SKILL.md: <one-line summary, or "unchanged">
- Scripts: <one-line summary per script, or "none -- pure prose skill">
- Scenarios run: <list, with pass/fail>
- Shape changes: <none, or the output-schema / field / CLI / exit-code deltas a
  consumer or surface would need to adapt to>
```

**Service:**

```
Updated service `<name>`:
- Change: <one-sentence>
- Routes affected: <list>
- Scenarios / tests run: <list, all pass>
```

Push it and stop. On approval, emit a `name: done` terminal report. In the
committed path a clean verification may produce no new worker commits -- that is
fine; the merge still brings the live commit forward.

## If no change is needed

If the right answer turns out to be "leave the artifact alone", emit a
`name: no-update-needed` terminal report. Do not commit a null change.

## If you need to give up

Emit a `stuck` terminal report per `harden-artifact.md`, stating what blocked
you and where the work stands.
