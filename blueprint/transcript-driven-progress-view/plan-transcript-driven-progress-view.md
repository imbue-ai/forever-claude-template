# Transcript-Driven Progress View

## Refined prompt

> **Redesign the chat progress view: transcript as source of truth, tk as enrichment**
>
> Replace the timestamp-window grouping in `turn-grouping.ts` with an in-order transcript walk that maintains a stack of open steps. Events render where they occur; events while no step is open go to a top-level ungrouped bucket (the proven plain-chat path).
>
> * **Structure** (which steps exist, order, open/close transitions, current step, grouping of tool calls) is derived **purely from transcript position** — the `tk` tool_result lines (`<id>` from create, `Updated <id> -> <status>` from start/close). No timestamps for grouping or ordering, no shell-variable parsing.
> * **tk is enrichment only**, a table keyed by id (`{title, close-summary, status, created}`) — needed anyway for titles and the canonical close summary (which isn't reliably in the transcript: it's in the truncated command input or set via `add-note`).
> * **(2b) Pending steps** (created but not yet transitioned in the transcript) are sourced from the enrichment table and render at the **tail** of the timeline, ordered by tk's own `created` field (single-source, fenced — never compared against transcript order). Transitioned steps position by their `Updated` line.
> * **Invariant (testable):** grouping of events and positioning of transitioned steps read transcript order only; the sole timestamp read anywhere is tk's `created`, used solely to order pending placeholders among themselves.
> * **(1a) Full-stack now:** `tickets_watcher.py` / `server.py` stop merging timestamped `task_event`s into the event stream; expose the keyed enrichment table instead.
> * **(8b) Drop regular tickets:** backend stops surfacing non-step tickets entirely; `parent_id` stays in the data but is unused — steps render flat, no parent/child nesting.
> * **(3a/6a) Carryover:** a step still open when the next user message arrives re-renders at the top of the new turn; the prior turn's node freezes (clamped to `in_progress`, static icon, independent state — never flips to done). Detection is transcript-native: a step is carried over if it is still on the open-stack when the walk crosses a user-message boundary. Visual-only freeze, no explicit label.
> * **(4/7a) Replies:** drop stop-hook reply-segmentation. Auto-promotion rule: text after the last **real (non-tk) tool activity** promotes to the below-timeline reply — covering both a wrap-up reply written *before* the closing `tk close` and a never-closed final step; text followed by more real work stays in-step narration; the close summary owns the step caption.
> * Fixes bug 1 (tool calls before the first step were dropped → they land in the ungrouped bucket and render inline) and bug 2 (in-progress step rendered at the top → it is positioned by its transcript transition, with no re-sort).

## Overview

- The progress view is fundamentally a **frontend for the transcript**. Today it instead folds events into per-ticket records, throws away transcript position, and reconstructs order/grouping/status from timestamps drawn from two unsynchronized clocks (Claude's session JSONL vs tk's ticket files). That reconstruction is the root cause of a recurring class of bugs (29 commits on `turn-grouping.ts`).
- Replace it with a single **in-order walk of the transcript** that maintains a stack of currently-open steps. Each event is placed where it occurs; tool calls and text group under whichever step is open, or fall into a top-level ungrouped bucket when none is open.
- **Structure comes only from the transcript.** Step lifecycle is read from the `tk` command tool_results already in the session stream: `tk create` prints the new `<id>`; every status change prints `Updated <id> -> <status>`. Order, grouping, open/closed state, and "which step is current" all fall out of position — no timestamp arithmetic.
- **tk is demoted to an enrichment side-table** keyed by id, supplying the canonical title and close summary (which the transcript can't carry reliably) plus the roster of pending steps. It decorates the transcript-derived skeleton; it never determines order or grouping.
- This structurally eliminates both known bugs and is guarded by a single testable invariant: the grouping/positioning code reads no timestamp except tk's own `created`, and only to order not-yet-started placeholders among themselves.

## Expected behavior

### Fixes the two known bugs
- **Tool calls before the first step no longer vanish.** Work the agent does before declaring any step renders inline at the top of the turn (exactly as today's no-steps plain-chat turns render), instead of being silently dropped.
- **The in-progress step renders in its real position**, never jumping to the top. A step appears where its start transition occurs in the transcript; there is no re-sort by start time or carryover/own split to get it wrong.

### Timeline structure
- A turn shows: any ungrouped pre-step work and prose, then the step nodes in the order their `tk create` / `tk start` transitions appear in the transcript, then pending (not-yet-started) steps as dashed placeholders at the tail, then the wrap-up reply below the timeline.
- Each step groups the assistant text + tool calls that occur while it is the open step; expanding a step shows that grouped work inline.
- Tool calls done while no step is open render in the top-level ungrouped bucket inline, in transcript order.

### Steps and status
- Step status follows the last transition seen in the walk: created-but-not-started = pending; after `Updated -> in_progress` = active; after `Updated -> closed` = done.
- The currently-active step is the last one started and not yet closed; only it shows the live spinner (when the turn is still running).
- Pending steps appear immediately when created (sourced from the enrichment table), as dashed placeholders at the bottom of the timeline, in tk creation order — so the user sees the whole declared plan up front.
- A step's title comes from tk enrichment; a done step's caption is its tk close summary. If enrichment hasn't loaded yet, the view degrades gracefully (bare id / no caption) rather than dropping the step.

### Carryover across turns
- A step left open (in_progress) when the next user message arrives re-renders as a fresh node at the top of the new turn and continues to collect that turn's work.
- The prior turn's node **freezes**: it keeps showing the state it had at that turn's end (active, static icon) and does **not** retroactively flip to done when the step later closes in the new turn. The same id renders as two nodes with independent state.

### Replies and prose
- The wrap-up reply (text after the last real, non-tk tool activity) renders below the timeline — including a reply the agent writes just before its closing `tk close`, and the case where the agent never closes the final step.
- Mid-work narration (text followed by more real work in the same step) stays attached to that step.
- Prose before the first step renders above the timeline; prose in a gap between steps renders inline at that point.
- Stop-hook feedback is no longer special-cased for reply placement; because the transcript is authoritative and walked in order, replies and chips land at their natural chronological position for free.

### Regular tickets
- Regular (non-step) tk tickets no longer render in the progress view at all. Only step records appear.
- Steps that carry a `parent_id` render flat at the top level (no nesting); the `parent_id` data is retained for possible future re-introduction.

## Changes

### Frontend — grouping (`turn-grouping.ts`)
- Replace the per-ticket fold + timestamp-window model with an **in-order transcript walk** that maintains a stack of open steps and assigns each event to the current open step or to a top-level ungrouped bucket.
- Derive step lifecycle from the transcript's `tk` tool_result lines (`<id>` on create, `Updated <id> -> <status>` on transition), anchored at their positions — not from `created_at` / `started_at` / `closed_at` timestamps.
- Delete the machinery that exists only to support reconstruction: active-window computation and capping, status clamping to partition boundaries, the carryover/own sort split, the started_at re-sort, and the `tk create` regex plan-order recovery.
- Source pending steps from the enrichment table; render them at the tail ordered by tk `created`. Dedup by id so a step that has transitioned is positioned by the transcript and removed from the pending tail.
- Reimplement reply placement with the simplified rule (text after the last real/non-tk tool activity → below-timeline reply; text followed by more real work → in-step narration); drop stop-hook reply-segmentation.
- Treat `tk` lifecycle commands as structural markers, not "real work," so they don't anchor reply detection and don't show as ordinary tool calls in a step body.

### Frontend — rendering (`ProgressBlock.ts`, `ChatPanel.ts`)
- Render the ungrouped top-level bucket inline (reuse the existing plain-chat assistant/tool rendering) so it is no longer conditional on the whole turn having zero steps.
- Render pending placeholder nodes at the tail; render carryover nodes at the top with frozen, independent state.
- Remove the no-steps-vs-steps branch that currently flips the entire section between plain-chat and progress rendering; the walk produces one unified structure (ungrouped bucket + steps) every time.
- Join enrichment (title, close summary) by id at render time.

### Backend — watcher and server (`tickets_watcher.py`, `server.py`)
- Demote the tickets watcher from emitting timestamped per-transition `task_event`s to exposing a **keyed enrichment table** (`id -> {title, summary, status, created}`).
- Stop surfacing non-step (regular) tickets entirely; keep only step records.
- Remove the timestamp normalization / transition-timestamp / mtime-fallback logic that existed to make task_events sortable into the merged stream.
- Stop merging task_events into the timestamped event stream in `server.py:_get_combined_events`; instead deliver step enrichment as a **separate, unpaginated snapshot** (`step_enrichment: {id: {...}}`) on the `/events` response, with SSE pushing per-id enrichment updates. The session transcript stream becomes session-events-only.
- `Response.ts` gains an `enrichmentByAgent` map alongside `eventsByAgent`; the walk joins enrichment by id at render time.
- Retain `step` and `parent_id` fields in the parsed data (parent_id unused for now).

### Agent protocol / tk
- No change required to `tk` for v1: the `Updated <id> -> <status>` and create-id outputs already exist and are tested in `vendor/tk/features/`. The redesign parses that existing, stable output contract.
- Optional hardening (flagged, not required): a `tk … --porcelain`/json output mode so the frontend parses a declared contract rather than human-readable lines.

### Tests
- Port the existing `turn-grouping.test.ts` behavior matrix (leading/inter-step/trailing prose, narration, carryover, reply promotion cases A2/A4/B2/B3/B4) onto the new walk; update A3 to reflect the new behavior (a wrap-up reply written before `tk close` now promotes).
- Add regression tests for the two fixed bugs: (1) tool calls before the first step render in the ungrouped bucket; (2) an in-progress step positioned after earlier closed steps, never at the top.
- Add a test enforcing the invariant: grouping/positioning reads no event timestamp except tk `created` for the pending tail.
- Capture one real failing transcript (both bugs) as a fixture before the rewrite, to prove the new model fixes actual data, not just the model of it.

## Open questions

Most prior items resolved by reading the code; findings are folded into the sections above. Recorded here with their resolution:

- **Enrichment delivery — resolved (separate snapshot).** `server.py:_get_combined_events` merges session + ticket events into one timestamp-sorted, position-paginated list (`/events` is tail-first + backfill; SSE pushes each new event). Because the transcript is position-paginated, a ticket event riding that stream could lag behind a step's `Updated` line on a freshly loaded tail. Resolution: deliver enrichment as a separate, unpaginated snapshot (always complete) and keep the transcript stream session-only. See Changes.
- **Frozen carryover keying — resolved, no new work.** Each turn already renders its own `ProgressBlock` instance, keyed by the section's user-message `event_id`, each with its own `expanded` state closure. The same ticket id rendered across two turns lands in two separate component instances — independent expand/spinner state, no Mithril key collision. The "frozen" status falls out of walking each turn's section with its own open-stack up to that section's boundary (the prior section never sees the later close).
- **Silent-create visibility — resolved.** `tk` prints `Updated <id> -> <status>` with the literal id on every transition (`vendor/tk/ticket:364`), so a step started via a shell variable is still positioned by a transcript line carrying its real id. The pending roster comes from the enrichment snapshot, not the transcript, so pending steps show regardless of whether the create id was echoed.
- **Turn-boundary detection — resolved.** The walk reuses the existing `isNonBoundaryUserMessage` classification (skill expansions, stop-hook, `/welcome`) so carryover is decided at true user-message boundaries, exactly as `ChatPanel` splits turns today.
- **Historical sessions — resolved (spot-check).** Old transcripts already contain the `tk` tool_results (create ids + `Updated` lines), so they render under the new walk; summaries for old steps come from the enrichment snapshot by id. Worth a spot-check on one pre-redesign session.
- **Pending-tail ordering — resolved.** Pending steps order by tk's `created`. Agents always run tk inside a Linux Docker container, where `tk`'s `_iso_date` (`vendor/tk/ticket:75`) uses GNU `date`'s `%6N` and writes **microsecond** resolution — so batched up-front creates get distinct, declaration-ordered timestamps. No macOS handling, sequence number, or tie-break is needed (tk is never run natively on macOS in this deployment).

No open decisions remain; the plan is ready for implementation.
