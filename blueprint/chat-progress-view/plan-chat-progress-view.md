# Chat Progress View — Canonical Specification

> **This is the single canonical spec for the chat progress view.** It supersedes
> and replaces four prior planning docs, which have been removed:
> - `chat-progress-view` (original: tickets-as-progress-markers, timestamp-window grouping)
> - `tk-step-ticket-model` (added `step: true`, multi-agent scoping, ticket/step nesting)
> - `transcript-driven-progress-view` (rewrite: transcript = structure, tk = enrichment side-table)
> - `end-of-turn-progress-rendering` (interjection + backward-scan reply rules)
>
> Those documents describe the evolution that produced the *current* code. This
> spec describes both where the feature is today and the **target simplification**
> agreed in design discussion. The target removes the tk enrichment watcher, the
> interjection/trailing-reply boundary machinery, and the dead ticket-nesting
> machinery — collapsing five layers and ~4,000 lines into a single
> transcript-driven path with a small set of decoration lines that tk prints on
> stdout. **Carryover (a step left open at a turn boundary re-rendering at the top
> of the next turn) is kept** — it is cheap (~35 lines), good UX, transcript-native,
> and keeping it removes the need for any auto-close/redeclare machinery.

---

## Overview

- **What the feature is.** For each user turn, the Minds chat renders a clean
  **progress timeline** instead of a raw tool-call stream: a vertical list of
  plain-English step nodes (pending / active / done), each expandable to reveal
  the underlying work, with the agent's wrap-up reply rendered below the
  timeline. Turns where the agent declares no steps render as ordinary chat.

- **Who drives it.** The agent declares and updates steps with `tk` — the
  vendored ticket tracker — using `tk create --step`, `tk start <id>`, and
  `tk close <id> "summary"`. Steps replace Claude Code's built-in TodoWrite
  (disabled), and unlike TodoWrite they render directly in the user-facing chat.

- **Core architectural decision (target).** The **agent session transcript is the
  single source of truth** for both *structure* (which steps exist, their order,
  their open/close transitions, which work groups under which step) and
  *decoration* (titles, close summaries). tk's lifecycle commands print
  machine-readable lines on stdout — `Created <id>: <title>`,
  `Updated <id> -> <status>`, `tk-step <id> title:`/`summary:` — that the parser
  protects from truncation. The frontend parses those lines out of tool output;
  nothing watches `.tickets/` files at runtime.

- **Why this design.** The previous design carried decoration in a separate
  `.tickets/`-watching backend (`tickets_watcher.py` + `tickets_parser.py`) that
  pushed an "enrichment" snapshot over SSE, joined onto transcript-derived
  structure by id. That side-table is the single largest source of complexity and
  is almost entirely redundant: every title and summary already passes through a
  tk command and is recoverable from the transcript. Folding decoration into tk's
  stdout deletes the watcher, the parser, the SSE enrichment channel, the
  timestamp-normalization code, the per-agent surfacing rules, and the
  deleted-directory resilience machinery — with no change to what the user sees.

- **Why tk stdout (not command-input parsing) is the primary contract.** Step
  ids are minted by `tk create` and only appear in *output*; the canonical close
  invocation uses a shell variable (`tk close "$S1" "..."`) whose id cannot be
  recovered from the command input at all. Since ids and transitions must come
  from output regardless, tk echoes the title/summary on the same output lines so
  id and decoration pair exactly, with no positional zipping and no shell-quoting
  parser to maintain. Command-input parsing is retained **only** as a
  best-effort fallback for historical transcripts (see below).

## Expected behavior

### From the user's perspective

- Every turn where the agent does real work shows a timeline of plain-English
  steps under the user's message, with the agent's final reply below it.
- Step status is visually obvious and limited to three states: **pending**
  (dashed ring), **active** (spinner on the live step; static partial ring once
  settled/idle/past), **done** (green check + one-line summary). There is no
  "failed" state — every step terminates as done.
- A done step shows a one-line, user-facing summary written by the agent
  ("Read through your recent commits to find the new theme").
- Expanding a step (chevron) reveals the assistant prose + tool-call blocks that
  occurred while that step was open, in transcript order, using the existing
  `tool-call-block` chrome.
- While the agent works inside a step, the latest in-step prose that is followed
  by more work shows as a live caption under the step ("narration").
- Work the agent does with **no step open** (e.g. before declaring any steps)
  renders inline as ordinary chat at its position in the turn — never dropped.
- Turns with no steps at all (chitchat, one-shots) render exactly as today's
  plain chat — no empty timeline, no forced ceremony.
- Non-genuine user messages (skill expansions, `/welcome`, stop-hook feedback)
  never split a turn; they are hidden or shown as inline chips.
- Existing chat history renders correctly: historical transcripts still contain
  the tk lifecycle lines, so the timeline reconstructs from them.

### From the agent's perspective

- TodoWrite is unavailable; `tk create --step` is the replacement for declaring
  plan steps. The first action on any substantive turn is to create all expected
  steps up front (batchable in one tool call), then `tk start` / work /
  `tk close` each in sequence. Only one step is in_progress at a time.
- The agent reads each step's id directly from the `tk create` output line and
  uses the literal id in `tk start` / `tk close`. (No shell-variable capture
  convention; `tk steps` re-lists ids if one scrolls out of reach.)
- `tk start` and `tk close` must each be the **only** command in their tool call
  (no `&&`, `cd` prefix, redirection) so the parser can hide the pure tk call and
  place the step cleanly. `tk create --step` calls may be batched.
- `tk close <id> "summary"` requires a one-line, user-facing summary of the
  *work done* in the step (not the outcome — the outcome goes in the final reply).
- At turn start, the existing reminder lists any of the agent's steps still open
  from a prior turn, so the agent can decide to continue, replace, or close them.
  Steps may be left open across turns (they carry over — see below).

### End-of-turn / reply placement (target — simplified)

- **Narration (kept):** prose inside a step that is followed by more work in the
  same step renders as the step's live caption.
- **Close-time ejection (replaces interjection + trailing reply):** when a step
  closes, any prose spoken inside it *after its last work* is ejected into the
  ungrouped inline stream immediately after that step node — so a closing remark
  the agent spoke just before `tk close` is promoted out of the step rather than
  buried in it.
- **Trailing reply:** the turn's wrap-up reply is simply the final contiguous run
  of ungrouped prose in the section, rendered below the timeline. It is not a
  separately computed concept — it falls out of the ejection rule plus
  "prose with no step open is ungrouped."
- Chips render at their chronological position but are not reply boundaries.
- This removes the three-way (leading / inter-step interjection / trailing)
  boundary computation and the chip-boundary interactions.
- **The `claude_tk_close_reoutput_nudge.sh` hook is removed**, not kept. Its
  premise is the *old* backward-scan reply rule — that prose written before a
  `tk close` stays buried inside the step, so the agent should re-output it after
  the close. Ejection inverts that premise: the pre-close prose is now
  automatically promoted out of the step and shown. Keeping the nudge would tell
  the agent to re-output text the renderer already displays, producing duplicate
  visible prose. So the ejection rule is the *complete* fix; no steering backstop
  is needed for this case.

### Unfinished steps at turn boundary (carryover — kept)

- A step left open (in_progress) when the next user message arrives **carries
  over**: it re-renders as a fresh node at the top of the new turn and continues
  to collect that turn's work. The prior turn's node **freezes** in place — it
  keeps the state it had at that turn's end (active, static icon) and does not
  retroactively flip to done when the step later closes. The same id renders as
  two independent nodes across the two sections, each with its own state.
- This is the existing behavior and is preserved unchanged. It is good UX: a user
  who sends a small clarification mid-task does not force the agent to restart or
  redeclare its steps; the work continues under the same step.
- Carryover is transcript-native: a step carries over iff it is still on the
  walk's open-stack when a user-message boundary is crossed. No timestamps, no
  hook coordination, and no auto-close are involved.
- This makes the design *simpler*, not just more capable: because steps carry
  over on their own, there is **no auto-close/redeclare mechanism** — no
  stop-hook auto-close, no runtime record file, no reminder rewrite. The existing
  `UserPromptSubmit` open-steps reminder and the soft non-blocking stop nudge stay
  as they are.
- Edge case (user message arrives mid-work): handled for free. The new message is
  just another boundary, so the open step carries over via the open-stack exactly
  as in the normal case — it does not depend on any hook having fired at turn end.

### Historical transcripts

- Old transcripts predate the new tk output lines, but their titles/summaries are
  present in the tk *command inputs* (`tk create --step "Title"`,
  `tk close <id> "summary"`), which the parser already recognizes by regex.
- The frontend keeps a **best-effort input-preview fallback**: when the new
  output decoration is absent, pull the quoted title/summary argument from the tk
  command's `input_preview`. This is explicitly allowed to be imperfect (exotic
  quoting degrades to a raw id) because it serves a frozen corpus only.
- To make the fallback reliable for the common batched-create case, the parser
  **exempts tk lifecycle commands from the 200-char `input_preview` truncation**
  (see Implementation). Because transcripts are re-parsed on every load, this
  applies retroactively.
- *Structure* (including carryover) is unaffected: old transcripts already carry
  the `Updated <id> -> <status>` lines, and carryover is preserved, so a step
  left open across turns in an old transcript still re-renders at the next turn's
  top exactly as it does today. Only decoration relies on the fallback.

### Out of scope

- Ticket/step nesting in the chat view (parent ticket as a node with steps
  nested under it). The tk `--parent`/auto-nest machinery, the `parent_id`
  plumbing, and the `.pv-tl-children` / `.pv-tl-node--ticket` CSS are **dead**
  and are removed. Regular (non-step) tk tickets do not render in the progress
  view.
- Retroactive enrichment from editing a `.tickets/` file after the fact (a
  consciously accepted loss — steps are ephemeral and turn-bound).
- A cross-agent task dashboard / sidebar.

## Implementation plan

### `vendor/tk/ticket`

- `cmd_create`: when `--step`, print `Created <id>: <title>` on **stdout** (today
  it prints the bare id). Keep bare-id stdout for regular (non-step) creates so
  existing `TICKET_ID=$(tk create ...)` captures in the lifecycle skills are
  unaffected.
- `cmd_start` (steps only): after `Updated <id> -> in_progress`, also print
  `tk-step <id> title: <title>`.
- `cmd_close` (steps only): after `Updated <id> -> closed`, also print
  `tk-step <id> title: <title>` and `tk-step <id> summary: <summary>`.
- Title/summary are emitted verbatim to end-of-line (newlines normalized to
  spaces); the id prefix disambiguates. No escaping/JSON.
- **Remove** the step auto-nest block (`--no-parent`, the in_progress-ticket
  scan, parent stamping for steps). `--parent` remains for regular tickets.
- Keep `--step`, `step: true` frontmatter, the `-step-` id segment (now the
  primary "is a step" signal), the `agent` creator stamp, and the mandatory
  close summary.

### `apps/system_interface/imbue/system_interface/session_parser.py`

- Extend `_truncate_tool_output` to also preserve `tk-step <id> ...` lines (same
  mechanism that protects `Updated <id> -> <status>` today).
- Exempt tk lifecycle Bash calls (anchored command-prefix regex, shared shape
  with the frontend's `TK_LIFECYCLE_RE`) from the 200-char `input_preview`
  truncation, so batched multi-create commands and long titles survive for the
  historical input fallback.

### `apps/system_interface/frontend/src/views/turn-grouping.ts`

- **Decoration source:** parse `Created <id>: ...` and `tk-step <id> title|summary: ...`
  from tool outputs into a per-id decoration map built in **one global pass over
  all events** (not per-section), so a carried-over node can look up the title
  parsed from its `tk start`/`create` line in an earlier turn. No external
  enrichment argument.
- **Input fallback:** when a decoration is missing, extract the quoted
  title/summary from the originating tk command's `input_preview` (best-effort).
- **Membership:** a transition id is a step iff it matches `-step-` (or, for
  historical ids, its `tk create --step` call is visible in the loaded window).
- **Pending roster:** a step whose `Created` line was seen but which never
  transitioned renders as a pending placeholder, ordered by transcript position
  (the `created_at` timestamp sort is deleted).
- **Prose:** keep narration; delete the `interjection` item kind,
  `trailingProseIds` / `interjectionIds`, and the three-way reply boundary.
  Implement close-time ejection + "trailing reply = final ungrouped prose run."
- **Carryover (kept):** `carryover`, `openStepsAtEnd`, `is_carryover`, and
  section-top re-opening are **retained unchanged**. The only adjustment is that
  the carried-over node's title/summary now come from the global decoration map
  (above) instead of the deleted enrichment table.
- **Deletions:** the `enrichment` parameter and all joins on it; `file_missing`
  handling.

### `apps/system_interface/frontend/src/models/Response.ts` and `StreamingMessage.ts`

- Delete `StepEnrichment`, the `#enrichment` store, `applyEnrichment` /
  `applyEnrichmentSnapshot` / `getEnrichmentForAgent`, the `step_enrichment`
  field on the events response, and the `step_enrichment` SSE message handling.

### `apps/system_interface/frontend/src/views/ProgressBlock.ts` + `style.css`

- Remove the `file_missing` "?" marker UI and its tooltip/aria handling.
- Remove the dead `.pv-tl-children` / `.pv-tl-node--ticket` CSS.
- Keep the interjection render path only if the item kind survives — under the
  ejection model, ejected prose renders as an ordinary ungrouped block, so the
  dedicated `.pv-interstep` block can be removed (confirm against mocks).

### `apps/system_interface/frontend/src/views/ChatPanel.ts`

- Update `buildSections` call site to drop the enrichment argument; everything
  else (virtualization, buildRows) is unchanged.

### Backend deletions

- Delete `tickets_watcher.py`, `tickets_parser.py`, and their `_test.py` files.
- Remove `server.py` wiring: `_get_or_create_tickets_watcher`,
  `app.state.tickets_watchers`, the `step_enrichment` field on GET `/events`, and
  the shutdown teardown loop.

### Hooks + CLAUDE.md (`scripts/`, `CLAUDE.md`)

- `claude_open_tickets_stop_nudge.sh` and `claude_open_tickets_reminder.sh`:
  **unchanged.** Carryover means there is no auto-close to perform; the existing
  reminder (lists this agent's still-open steps) and soft stop nudge keep working
  as they do today.
- CLAUDE.md: keep the carryover semantics (steps may stay open across turns; the
  start-of-turn keep/replace/close triage); remove the auto-nest /
  ticket-nesting-in-chat-view sections; rewrite the plan-declaration examples and
  the `launch-task` example to the literal-id (no `$(...)` capture) style; keep
  the standalone-command rule and the close-before-reply guidance.
- Keep `claude_tk_standalone*` (the standalone-command enforcement, which buys
  clean hiding of pure tk calls).
- **Remove `claude_tk_close_reoutput_nudge.sh`** and its single
  `.claude/settings.json` PreToolUse wiring. Under close-time ejection its advice
  would cause duplicate visible prose (see Expected behavior → end-of-turn).

## Implementation phases

1. **tk output contract + parser protection (additive, safe alone).** tk prints
   the new lines; `session_parser.py` protects them and un-truncates tk inputs.
   The current frontend ignores the new lines (the `Updated` regex is unanchored),
   so this lands without breaking anything.
2. **Frontend rewrite + backend deletion (one change).** Switch
   `turn-grouping.ts` to transcript-only decoration (global-by-id map) with input
   fallback; delete the enrichment store/SSE/watcher/parser together so there is
   no half-state. Includes prose simplification. Carryover is retained.
3. **Protocol (independent, lands last).** CLAUDE.md and skill/example updates
   (literal-id style); remove tk auto-nest and dead CSS. No hook behavior change.

## Testing strategy

- **Unit (`turn-grouping.test.ts`):** rebuild the behavior matrix on
  new-format synthetic events — titles, summaries, pending roster (transcript
  order), narration, close-time ejection, trailing reply, carryover (a step open
  at a boundary re-renders at the next turn's top while the prior node freezes),
  ungrouped pre-step work, chips. No enrichment argument anywhere.
- **Historical fixture:** a section built from a real pre-redesign transcript
  (old tk output, a batched `>200`-char create command) renders titles and
  summaries via the input fallback.
- **Backend:** assert `step_enrichment` appears nowhere and GET `/events` no
  longer carries it; delete the watcher/parser tests.
- **tk:** update/add coverage for the new `Created`/`tk-step` output lines and
  confirm regular-create stdout is unchanged.
- **Manual (real app):** run a multi-step turn and confirm the rendered timeline
  matches today's; leave a step open across a follow-up user message and confirm
  it carries over to the next turn while the prior node freezes.
- **Full suites before done:** `npm run lint`, `npm run test`, and
  `uv run pytest` for `apps/system_interface`.

## Open questions

- **Pending placeholders for steps created out-of-window.** Under transcript-only
  parsing, a pending step whose `Created` line has scrolled out of the loaded
  event window won't show until it transitions. Steps are turn-bound and
  creator-private, so this is believed acceptable — confirm during implementation
  against a long-transcript spot check.
- **`.pv-interstep` removal.** Confirm against regenerated mocks that ejected
  closing prose reads acceptably as a plain ungrouped block, so the dedicated
  broken-thread style can be deleted rather than retained.
- **Accepted regressions (decided, recorded):** editing a `.tickets/` file after
  the fact no longer updates the chat; very old steps (pre-`-step-` ids) whose
  create call has scrolled out of the loaded window drop from historical
  timelines.

## Related (not superseded)

- `blueprint/scaling-design/` covers the virtualized list / pagination and is a
  separate concern. It references the old `step_enrichment` response field in its
  description of the `/events` shape; that reference is stale once Phase 2 lands
  and should be read as historical.
