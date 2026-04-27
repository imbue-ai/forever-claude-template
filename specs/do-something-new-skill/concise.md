# /do-something-new skill

## Overview

- Add a `/do-something-new` skill scoped to net-new tasks the agent hasn't done before, where no single existing skill applies and the work needs nontrivial research/exploration/experimentation.
- Driving principle 1: when the user is actively interacting, return results they care about *fast*.
- Driving principle 2: the user cares about the experience, not technical details.
- The skill is much lighter than `crystallize-task` / `heal-skill` / `update-skill`. It does the bare-minimum version of the task as fast as possible, validates the core capability, then crystallizes in the background while pivoting to interface design.
- Implicit triggering via skill description; the skill's first step is a quick existing-skill scan that bails only when a *single* skill covers the *whole* ask.
- Out of scope: tasks where an applicable single skill already exists (use it), and pure code-writing/dev work.
- Companion changes touch CLAUDE.md (a principle line plus a "Using crystallized skills" bullet) and `crystallize-task` (data-capture guidance + artifact handoff).

## Expected Behavior

### What the user sees

- Asks for something new (e.g. "fetch my emails", "fetch my emails and slack and generate connections between them"). Agent triggers `/do-something-new` implicitly.
- Sees at most a couple of essential clarification questions — never an exhaustive Q&A.
- Sees a short proposed plan: auth method + 3-5 process steps + one line on what they'll see at the end. Approves or pushes back.
- For browser auth: gets a heads-up like "need to launch the latchkey setup flow — sign in in the pop-up browser", then the popup opens. For `latchkey auth set` cases: gets the exact command to run.
- Latchkey setup is part of the normal flow, not a failure. If setup itself fails or post-setup calls don't work, the agent surfaces a specific cause AND proposes 1-2 concrete alternatives before the user chooses.
- Sees a data sample first — even if their original prompt asked for a UI/dashboard. Default presentation: brief natural-language summary ("Fetched 5 emails: 'Re: Q3 plan' from Alex, …"), with raw JSON saved to a file the user can ask to see; agent may pick a different format based on context or the user's apparent technical preference.
- On approval of the data sample, the agent says something like "Great, let me convert this into a robust reusable workflow" and immediately pivots to interface design:
  - If the user named a desired interface in the original prompt: "let's talk more about the interface you asked for".
  - If not: "let's talk about how you'd like to interact with this".
- The skill's responsibility ends with that handoff. Interface design happens in subsequent turns.
- If the user rejects the data sample ("this isn't what I wanted"), the agent re-proposes a plan; it only re-runs the small research pass if the new ask requires it.
- If the user asks to re-fetch while a background crystallize is running, the agent just runs the manual flow again. The in-flight crystallize keeps running and lands normally; the two are not in conflict.

### What's happening behind the scenes

- Quick existing-skill scan; bail only if a single skill covers the whole ask. Partial matches → continue with this skill but reuse the existing skill for whatever subset it handles.
- *Small* research pass: `latchkey services list --viable`, `latchkey services info <svc>` for any obviously-involved service, brief web/docs scan if needed. Capped at ~2-3 minutes / a handful of tool calls. Goal: don't bullshit about feasibility, but don't take long.
- Core-capability validation runs *first* before any other work. For multi-service asks, each underlying service is validated independently, then the combined operation. Fail-fast on whichever fails first.
- Ad-hoc scripts written during the flow live under `runtime/do-something-new/<slug>/` (gitignored). Code is kept simple — fast > polished. Inline bash or `uv run python -c` is fine when it works; substantive scripts get files.
- On data-sample approval, the agent launches `crystallize-task` in the background, with the calling skill's runtime dir handed off as input artifacts (see Changes).
- Background crystallize gates:
  - Gate 1 (outline-approval): the lead filters — only escalates a *genuine process question* to the user, suppressing technical-detail questions it can decide itself.
  - Gate 2 (final-artifact): surfaced normally, deferred until the user isn't actively working on something else (existing lead-proxy "do not interrupt more recent user work" rule already covers this).
- If a re-fetch reveals something the worker would need (wrong endpoint, missing auth scope, mind-changed about which fields matter), the agent `mngr message`s the worker. Otherwise stays silent — no automatic post-every-refetch ping.

## Changes

### New: `.agents/skills/do-something-new/SKILL.md`

- Description triggers implicit invocation, drafted as: "Use when the user asks you to do something net-new — a task you haven't done before, no existing skill applies, and getting it right will require nontrivial research, exploration, or experimentation. Skip when an applicable single skill already exists or for pure dev/code-writing work."
- Skill body documents the flow above, structured as numbered steps:
  - **Step 0 — existing-skill scan.** Match against existing skill descriptions. Bail with a short user-facing note if a single skill covers the whole ask. Partial matches → continue but reuse the matching skill for its subset.
  - **Step 1 — essential clarifications.** Ask only what's blocking. Zero is fine. Goal is to unblock work, not to gather complete requirements.
  - **Step 2 — small research pass.** Concrete commands: `latchkey services list --viable`; `latchkey services info <svc>` for any obviously-involved service; optional 1-2 web searches / docs reads if a service isn't covered. Cap: ~2-3 minutes / handful of tool calls.
  - **Step 3 — propose plan.** Two-section format ("Auth: …" line + numbered process steps), with a one-line "what you'll see at the end". Browser auth → include the heads-up wording. `auth set` → include the exact command for the user to run.
  - **Step 4 — validate the core capability first.** For multi-service asks, validate each service independently before the combined operation. Latchkey setup is part of the normal flow. On failure, surface specific cause + 1-2 concrete alternatives before the user chooses.
  - **Step 5 — generate the data sample.** Default to brief natural-language summary; save raw JSON to `runtime/do-something-new/<slug>/sample.json`. Agent may pick another format if context warrants.
  - **Step 6 — launch background crystallize and end with interface handoff.** Kick off `crystallize-task` (with `source_artifacts_dir: runtime/do-something-new/<slug>/` in the task frontmatter so artifacts get pushed to the worker — see crystallize-task changes below). Then end the skill with one of the two interface-reminder phrasings, depending on whether the user named an interface in the original prompt.
  - **Re-fetch handling.** Documented as the manual-flow fallback. Agent decides per-occasion whether to `mngr message` the crystallize worker; only does so when the worker would actually need the new info.
  - **Rejection handling.** If the user rejects the data sample, re-propose plan (Step 3); re-research (Step 2) only if the new ask requires it.
- No `scripts/` directory — the skill's work is conversational + ad-hoc. Ad-hoc scripts live in `runtime/`, not in the skill.

### Edit: `.agents/skills/crystallize-task/SKILL.md`

- Append to the worker task body (Step 3 in the existing skill): "When fetching data from external APIs, capture *all reasonable fields per record* in the calls you're already making, not just the fields the user displayed. Pagination is a normal part of the workflow if the ask requires it. Do NOT make extra un-asked-for API calls just to gather more data." Applies regardless of how crystallize-task was triggered.
- Add an optional task-file frontmatter field `source_artifacts_dir`. When present, the lead pushes that directory to the worker (`mngr push <worker>:<dir>/ --source <dir>/ --uncommitted-changes=merge`) alongside its own `runtime/crystallize/<name>/` push. Without the field, existing invocations work unchanged.
- Update `crystallize-task/SKILL.md` Step 4 to include the extra `mngr push` call, gated on the frontmatter field. Document that callers (like `/do-something-new`) populate this field to hand off pre-existing scripts and sample data.

### Edit: `CLAUDE.md`

- Under "Always remember these guidelines", add a bullet: "When the user is actively interacting with you, prioritize delivering a result they care about over technical polish. Technical refinement can happen in the background."
- Under "Using crystallized skills", add a bullet: "If the user asks for something net-new, no existing skill applies, and it'll need research or experimentation: invoke `/do-something-new`."
