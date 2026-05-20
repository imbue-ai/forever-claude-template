# tk step / ticket model — multi-agent scoping

## Overview

- The chat-progress-view feature has been using tk tickets as ephemeral per-turn progress markers, which works in a single-agent setup but breaks down the moment two agents share a TICKETS_DIR: agent B's `tk ready` and the open-tickets hooks surface agent A's turn-markers as if they were B's pending work.
- Resolution: tk stays tk for canonical cross-agent issue tracking, and a thin layer formalises the progress-marker use case as a distinct kind. One new frontmatter field — `step: true` — distinguishes a turn-bound progress record from a regular tk ticket. Defaults stay tk's defaults; the step concept is additive.
- The progress-view UI shows two surfaces in one timeline: the agent's open **step records** (the existing chat-progress flow), and any **regular ticket currently in_progress and assigned to this agent** (the agent picked it up). Steps can be parented to a ticket (`--parent=<id>`) to render visually nested under it. Auto-nest infers the parent when this agent has exactly one ticket in `in_progress`.
- Cross-agent surfaces (`tk ready`, `tk ls`, partial-id resolution, the open-tickets hooks) gain agent-scope discipline: step records are visible only to their creator; regular tickets stay visible to all. Assignment becomes the load-bearing signal for "this is now my work": `tk start` auto-self-assigns the current agent if unassigned.
- CLAUDE.md's "Task management" section is rewritten end-to-end to teach the new model. The progress watcher, the open-tickets hooks, and the frontend turn-grouping / ProgressBlock all gain step- and assignee-awareness.

## Expected behavior

**Single-agent (today's flow continues to work):**
- Agent at turn start runs `tk create --step "..."` for each step in their plan. All start as `open` (rendered "pending" / gray dashed ring). Agent then `tk start` / `tk close <id> "summary"` each in turn. UI shows the spinner → checkmark progression.
- Open steps remaining at turn end carry over to the next turn (existing carryover logic in turn-grouping.ts unchanged), and surface in the UserPromptSubmit reminder hook.

**Two chat-level agents in the same worktree (the bug being fixed):**
- Both share `TICKETS_DIR=/code/runtime/tickets`.
- Agent B's `tk ready` shows only regular tickets (no steps), regardless of which agent created them.
- Agent B's UserPromptSubmit reminder shows only B's own open step records — never A's.
- Agent B's `tk show <partial-id>` partial-match resolution prefers regular tickets; matches against step records require an `--include-steps` flag or a full id.
- The progress timelines for A and B are disjoint: each sees only their own steps (creator-scoped) and only the tickets they themselves are currently working (`assignee == me AND status == in_progress`).

**One agent picks up another agent's ticket:**
- Agent A files a ticket: `tk create "Fix auth race"`. `assignee:` is left unset (when `MNGR_AGENT_NAME` is set, the default-to-`git user.name` behaviour is suppressed). `agent: A` is stamped as creator.
- Agent B runs `tk start <id>`. tk auto-self-assigns B (`assignee: B`). The ticket now surfaces in B's progress view because `assignee == B AND status == in_progress`. It no longer surfaces to A (A is neither assignee nor working it).
- If A had still been assigned and B runs `tk start`, tk warns about the reassignment but proceeds.

**Multi-step work on a picked-up ticket:**
- B does `tk start <ticket-id>`, then `tk create --step "Read the middleware"`. The step is auto-nested under the ticket (B's exactly-one in_progress ticket). UI renders the ticket as a parent node with the step indented under it.
- Subsequent `tk create --step` calls in the same turn auto-nest under the same ticket. Once B closes the ticket, future `tk create --step` calls return to standalone (no in_progress tickets to auto-nest under).
- If B has two tickets in_progress simultaneously, `tk create --step` without explicit `--parent` errors out with a list of `<id> — <title>` lines for each in-progress ticket; the agent must specify `--parent=<id>` or `--no-parent`.

**One-step task (no separate flag needed):**
- B does `tk create "Bump dep X"` then `tk start <id>` then `tk close <id> "Bumped, tests pass"`, no `tk create --step` calls in between. UI renders the ticket as a single flat node (no child steps). No special "both" mode needed.

**Multi-turn ticket continuation:**
- B picks up a ticket in turn T1, files 2 child steps, ends without closing. In turn T2, B files a third child step under the same parent.
- T1's progress block shows the parent + 2 steps. T2's progress block re-renders the parent (with a `continues_forward` indicator) + the third step. The carryover mechanism already handles per-task carryover; extension is to keep the parent-children grouping intact while carrying over.
- Turn attribution for a regular ticket B picked up = the first turn in which B emitted any event for the ticket. (Today's `created_at`-based attribution would put it in the originating agent's turn — wrong for picked-up tickets.)

**Hooks:**
- UserPromptSubmit reminder shows only this agent's open step records, never tickets. The text is reworded to remove the "if a ticket appears that you didn't start, just close it" guidance (which currently fires across agents).
- Stop hook nudges if open step records remain at turn end; does NOT auto-close them. Count and IDs come from this agent's step records only.

**Cross-agent observability (in-scope as CLI, no UI):**
- Other agents' tickets stay visible cross-agent through normal `tk ready` / `tk ls`. Other agents' STEPS are invisible by default. An opt-in flag (e.g. `tk ls --steps --all-agents`) exposes them for debugging or "what is agent A currently doing?" peeking. This is CLI-only — no UI sidebar in this plan.

**Out of scope:**
- Backfill of `step: true` onto pre-existing tickets. On rollout, in-flight chat-progress tickets remain as regular tickets and stop rendering in the progress view; agents file fresh step records going forward.
- Assignee history tracking.
- A cross-agent task dashboard / sidebar in the frontend.
- Repurposing or removing tk's existing `type:` field values.

## Changes

### vendor/tk/ticket (core script patches)

- `cmd_create`: Stamp `step: true` into frontmatter when `--step` flag is passed. Resolve auto-nest at create time: when `--step` is set, no `--parent` was given, and `MNGR_AGENT_NAME` is set, scan for this agent's open tickets in `in_progress` status. Zero → no parent. Exactly one → set as `--parent`. More than one → emit an error listing `<id> — <title>` for each candidate; exit non-zero unless `--no-parent` was passed.
- `cmd_create`: When `MNGR_AGENT_NAME` is set, skip the `git config user.name` default for `assignee`. Tickets are unassigned until `tk start` or `tk assign`.
- `cmd_close`: Accept an optional positional summary argument. Required when closing a step (error if absent for step:true tickets); optional for regular tickets (when supplied, equivalent to `add-note` + close). The summary becomes the watcher-visible final note that the chat UI renders.
- `cmd_start`: Detect step vs. ticket. When starting a regular ticket: if `assignee` is unset, auto-self-assign `$MNGR_AGENT_NAME`. If `assignee` is set to another agent, print a warning to stderr ("Reassigning from <other> to <me>") but proceed and overwrite.
- `cmd_ready`, `cmd_ls` (in `ticket-extras`), `cmd_closed`, `cmd_blocked`: Filter out tickets where `step == true` by default. Add `--include-steps` / `--only-steps` flags for explicit inclusion.
- `ticket_path` (partial-id resolution): prefer matches against non-step tickets when a partial id matches both. (Full-id matches remain unambiguous.)
- `cmd_show`: Render a new `## Steps` section (children with `step: true`) in addition to the existing `## Children` section (children without `step: true`). The `## Children` section excludes steps to keep regular-ticket parentage clean.
- Add `cmd_assign` and `cmd_unassign` built-in subcommands for explicit assignment. `tk assign <id> [<agent>]` defaults `<agent>` to `$MNGR_AGENT_NAME` when omitted.

### vendor/tk plugin or core listing for steps

- `tk steps` (or `tk ls --only-steps`): list this agent's open step records. The hook reminder uses this exact command. Decision can be made during implementation; default proposal is to add a `tk-steps` plugin under `vendor/tk/plugins/` to avoid bloating core.

### vendor/tk tests (behave features)

- Scenarios for `tk create --step` with and without `--parent`.
- Auto-nest scenarios: zero / one / many tickets in_progress.
- `tk close <id> "summary"` accepts positional summary; step-without-summary errors.
- `tk ready` / `tk ls` exclude steps by default; `--include-steps` flag works.
- `tk show <parent-id>` renders separate `## Steps` and `## Children` sections.
- `tk start <id>` warns and reassigns when ticket already assigned to a different agent.
- `tk create` leaves `assignee` unset when `MNGR_AGENT_NAME` is set.

### apps/system_interface/imbue/minds_workspace_server/tickets_parser.py

- Extend `TicketState` with `step: bool` (parsed from frontmatter; absent → `False`), `parent_id: str` (parsed from `parent:` field; absent → empty string), and `assignee: str` (already in frontmatter; surface it). Confirm field defaults work with existing pre-format tickets.

### apps/system_interface/imbue/minds_workspace_server/tickets_watcher.py

- Replace the existing creator-only agent filter (line 237) with the new dual rule:
  - If `state.step == True`: surface to creator (agent == self._agent_name). Same as today's filter for steps.
  - If `state.step == False`: surface to assignee (assignee == self._agent_name). Tickets surface in any agent's stream as long as that agent is the assignee.
  - Tickets with empty step AND empty assignee fall through both rules and are dropped (backwards-compat: pre-existing tickets without `step:`/`assignee:` may need an explicit migration toggle or display-everywhere fallback during rollout — decide during implementation).
- Add `parent_id`, `step`, `assignee` fields to the `_make_event` payload so the frontend gets them on every transition.
- No structural change to event ordering / replay logic.

### apps/system_interface/frontend/src/models/Response.ts

- Extend `TranscriptEvent` task_event fields with `step?: boolean`, `parent_id?: string`, and `assignee?: string`. Threaded through the existing flat event store unchanged.

### apps/system_interface/frontend/src/views/turn-grouping.ts

- Fold `parent_id`, `step`, `assignee` into `TaskRecord`.
- Change turn-attribution: a `TaskRecord` is owned by "the earliest turn whose window contains an event for this ticket by the current agent" rather than by the creation timestamp. (For step records this reduces to "the turn containing the step's `created_at`" since steps are creator-emitted; for regular tickets picked up by this agent it correctly attributes to the agent's first action.)
- Group child step records under their parent ticket before turn rendering. Emit a new `TaskInTurn`-shaped structure that includes either `children: TaskInTurn[]` for parent tickets or a flat node for unparented steps.
- Carryover: keep the existing "carry every unfinished task to subsequent turns until closed" rule, but carry the entire parent + children grouping as a unit (so T2's progress block shows the parent header re-rendered with all already-folded child steps, plus any new children added in T2).

### apps/system_interface/frontend/src/views/ProgressBlock.ts

- Render parent tickets with distinctive chrome: a small id badge (e.g. `[abc-1234]`), slightly different background or border, and the title styled to indicate "ticket" vs "step."
- Render child step records visually indented under their parent, sharing the same vertical thread but offset (and slightly de-emphasized typography) so the hierarchy reads at a glance.
- Standalone (parentless) steps render flat as today.
- UI status icons reused: pending (gray dashed), in_progress (spinner / static partial ring on `continues_forward`), done (filled check).
- The "expand to reveal tool calls" affordance continues to work per-node (parent ticket's own events vs. each child step's own events).

### apps/system_interface tests (frontend + backend)

- Frontend turn-grouping tests: new attribution rule (picked-up ticket lands in the picker's turn, not the creator's); auto-nest grouping; parent + children carryover across multiple turns; behaviour when a ticket is created and immediately picked up by another agent in the same turn.
- Frontend ProgressBlock tests: nested rendering for parent + steps; flat rendering for standalone steps; distinct chrome for ticket vs. step.
- Backend parser tests: `step` / `parent_id` / `assignee` field parsing.
- Backend watcher tests: assignee-based filter for tickets, creator-based filter for steps, the assignee-and-step-both-absent fallthrough case.

### scripts/claude_open_tickets_reminder.sh and scripts/claude_open_tickets_stop_nudge.sh

- Replace `tk ready` invocation with whichever step-listing command lands (`tk steps` or `tk ls --only-steps`). Re-export `TICKETS_DIR` as today.
- Update the reminder text: remove guidance to "close tickets that appear that you didn't start" (was unscoped, will be wrong cross-agent); replace with guidance specific to step records — keep working / close / replace, per the existing protocol but scoped only to steps.
- Stop hook count + IDs likewise scope to step records only.

### CLAUDE.md

- Rewrite the "Task management (CRITICAL — read this before doing real work)" section end-to-end. New content covers:
  - The step vs. ticket distinction. When each is appropriate (step: ephemeral within-turn progress markers; ticket: substantive work worth tracking cross-agent).
  - The auto-nest rule and the multiple-in-progress-tickets convention (allowed but discouraged; require explicit `--parent` when ambiguous).
  - The end-of-turn lifecycle: close every started step with `tk close <id> "summary"` (positional summary required). Tickets can be left in_progress across turns; carryover surfaces them in the next turn.
  - How to file vs. pick up real tickets (`tk create` to file, `tk start` to pick up; auto-self-assignment).
  - Hook reminders show only step records now -- tickets are managed through `tk ls`, `tk ready`, `tk show`, etc. that the agent invokes itself.

### Documentation hygiene

- Update vendor/tk/CLAUDE.md and README.md briefly to note the new `--step` flag, the `## Steps` section in `tk show`, and the assignee default change when `MNGR_AGENT_NAME` is set.
- Update the chat-progress-view planning doc (blueprint/chat-progress-view/) with a forward-reference to this plan, since the original assumed steps == tickets.
