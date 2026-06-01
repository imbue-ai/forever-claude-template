# Plan: End-of-turn progress-view rendering rework

> **Robustly handle all end-of-turn rendering scenarios in the chat progress view, without relying on agents closing steps correctly.**
>
> * **Guiding principle:** the progress view does the right thing whether or not steps get closed; agents shouldn't have to fight the system. Rendering is the load-bearing fix; prompt/hook steering is best-effort only.
> * **Reply detection (backward scan):** scan backward from the end of a turn collecting text-only assistant messages; stop at the first `closed` task event OR tool activity. That trailing run is the user-facing reply, promoted to the top level.
>   * Consequence: "close last step, then write the wrap-up" promotes cleanly (the ideal path). "Speak, then close" leaves the message in-step; a `tk close` nudge catches this and prompts the agent to re-output user-facing text after the close.
> * **Position-aware top-level messages:** leading text (before the first step) renders *above* the timeline; inter-step text (after a close, before the next step starts) *interrupts* the timeline inline as a broken-thread, full-width block (Variant C — the thread visibly stops, the prose spans the column, the thread resumes); the trailing reply renders *below* the timeline. No top-level text is ever hidden under a step.
> * **Unclosed steps at idle:** drop the internal-jargon "settled" tag; keep the static partial-ring icon; show no caption when there's nothing to show; any surviving caption is static + italic + muted (no shimmer).
> * **No-steps turns are out of scope:** the "ungrouped work" rendering is being dropped and reworked as a separate effort; this plan does not touch or rely on it.
> * **Steering (best-effort):** claude.md guidance = "close your final step *before* writing your wrap-up reply," with a short why. Existing PreToolUse step-creation nudge and non-blocking Stop nudge stay as-is.
> * **`tk close` nudge (not a flag):** when text was emitted between the last tool call and the close, a Claude Code hook on the `tk close` invocation reminds the agent that the user won't see that text by default and to re-output it if it was a general/user-facing message. No `tk close` flags.
> * **Validation:** regen the HTML mocks first (with side-by-side variants for the open inter-step visual), discuss, then unit tests on the pure `turn-grouping` functions (the scenarios as cases); manual testing in the real app by the user.

---

## Overview

- The chat progress view currently decides which assistant messages render at the top level using a window-containment rule in `selectFinalMessages` (`turn-grouping.ts`): a text-only message is top-level if it falls outside every step's active window, plus the single last text-only message if its containing step is done or settled.
- That rule produces three concrete defects at end-of-turn, confirmed by tracing the code and mocked in `attachments/end-of-turn-scenarios.html`:
  1. **Dropped messages** — when multiple text-only messages land inside a done/settled step's window, only the last is promoted; earlier ones disappear from the top level (only visible by expanding the step).
  2. **Contextless unclosed steps** — a step left open when the agent goes idle shows a bare title plus a "settled" tag (internal jargon), with no summary and no narration (narration is actively suppressed for settled steps).
  3. **Narration→reply visual jump** — a final message inside an unclosed step renders as italic shimmering narration while the agent is active, then jumps to a plain top-level block once the agent goes idle.
- Agents are unreliable about *when* they close steps; prompting does not fix this in practice. So the rendering layer must produce a clean result for every open/closed/idle/active combination.
- The fix replaces window-containment with a **backward-scan reply rule** plus **position-aware placement** of top-level text (above / interrupting / below the timeline). A `closed` event is a hard stop for the scan, which makes the ideal agent behavior simple to state and steer toward: close the last step, then write the reply.
- Steering (claude.md) reinforces that ideal but is explicitly not relied upon. A `tk close` hook nudges the agent to re-output user-facing text that would otherwise be stranded before a close; no `tk` flags are added.

## Expected behavior

Scenario references map to `attachments/end-of-turn-scenarios.html`.

- **Reply detection is a backward scan.** From the last event of a turn, walk backward gathering text-only assistant messages. Stop at the first `closed` task event or any tool activity (a tool call or tool result). The gathered run (in chronological order) is the turn's top-level reply.
  - Close-then-speak (A2): the post-close message is the reply → rendered below the timeline.
  - Speak-then-close (A3): the message precedes the close, so the scan stops at the close and it is not promoted on its own — it stays in-step (expandable). The `tk close` nudge fires here, prompting the agent to re-output the text after the close (turning it into the A2 case) when it was meant for the user.
  - Speak / more tools / speak (B3): only the trailing message (after the last tool result) is the reply; the earlier message remains in-step narration (it was followed by tool activity), not dropped from the timeline's expandable body.
  - Speak, close, speak again (A4): the scan stops at the close, so only the post-close message is promoted; the pre-close message stays in-step, and the close nudge applies as in A3.
- **Top-level text is placed by position, never hidden:**
  - **Leading** text emitted before the first step is created renders *above* the entire timeline as plain prose.
  - **Inter-step** text emitted after a step closes and before the next step starts *interrupts* the timeline at that chronological point as a broken-thread, full-width block (Variant C): the vertical thread stops above the block, the prose spans the column, and the thread resumes below for the next step.
  - **Trailing** reply renders *below* the timeline as plain prose.
- **Unclosed steps when the agent is idle (S5, S6, S8):**
  - No "settled" tag.
  - Static partial-ring icon (unchanged glyph) signals "was in progress, stopped."
  - If a promoted reply exists below, the step shows no caption (the reply carries the meaning). If some in-step narration survives and there's no better context, it may render as a static, italic, muted caption (no shimmer).
- **Active (streaming) turns are unchanged in spirit (S9–S12):**
  - In-step narration still shows as the live shimmering caption while the agent works.
  - Once the agent goes idle, the trailing reply is promoted below the timeline; because the backward-scan result is positional rather than tied to `is_settled` narration promotion, the active→idle transition no longer produces the jarring narration→reply jump (the final message is consistently a top-level block).
- **No-steps turns (no step records declared):** out of scope. The existing "ungrouped work" rendering is left untouched by this work and will be replaced by a separate effort; this plan neither enhances nor depends on it.
- **Steering:** claude.md tells agents the ideal is to close the final step before the wrap-up reply, and briefly explains why (so the reply lands after the last close and is promoted). The `tk close` nudge is the safety net, not a default to rely on.

## Changes

Relative to the existing system, without implementation detail:

- **`turn-grouping.ts` — replace the reply-selection model:**
  - Replace `selectFinalMessages`'s window-containment logic with the backward-scan rule (stop at first `closed` event or tool activity), returning the trailing text-only run.
  - Add a notion of message *position* (leading / inter-step / trailing) so the renderer can place top-level text above, within, or below the timeline rather than always below.
  - Adjust `attributeNarration` so in-step narration is the text-only messages that were followed by tool activity in the same step (mid-work narration), decoupled from the `is_settled` suppression that currently blanks unclosed-idle steps.
  - Revisit `stepActiveInWindow` / window-end computation only as needed to support positional classification; keep the serial-step invariant and trailing-tool-result pull-in.
- **`ProgressBlock.ts` — positional rendering:**
  - Render leading top-level messages above the timeline, inter-step messages interleaved at their position (Variant C broken-thread block), and trailing reply below.
  - Remove the "settled" carryover-style tag for unclosed-idle steps; keep the partial-ring icon; render captions only when there is genuine context, using a static (non-shimmer) muted style for settled captions.
- **`style.css` — progress-view styles:**
  - Add the Variant C inter-step block style: opaque background that paints above the timeline thread (interrupting it) with top/bottom hairline rules; full-column-width prose. Must mask the thread cleanly without drawing over the text.
  - Add the leading-above message position style.
  - Add a static (non-animated) variant of the settled-step caption; ensure the shimmer is reserved for genuinely active narration.
- **claude.md — steering:**
  - Add concise guidance: close the final step before writing the user-facing wrap-up reply, with a one-line rationale tying it to how the progress view promotes replies. Frame as best-effort, not a hard requirement.
- **`tk close` nudge hook (new):**
  - A Claude Code hook on the `tk close` invocation that reads the conversation transcript (`transcript_path`). If text-only assistant output was emitted since the last tool call (i.e. it will be stranded inside the closing step under the reply rule), it injects a reminder: the user only sees that text on close inspection, so re-output it after the close if it was a general/user-facing message.
  - Lives as a hook (entry in `.claude/settings.json` + a script under `scripts/`), not inside `tk` itself, since `tk` has no access to the transcript. Mirrors the existing `claude_require_steps_pretool.sh` / `claude_open_tickets_stop_nudge.sh` pattern.
  - Fires on any close with dangling pre-close text (including mid-turn closes), since that text is stranded regardless of position.
- **Mocks — `attachments/end-of-turn-scenarios.html`:**
  - Covers the full scenario matrix under the new rules. Inter-step variant comparison retained for reference with **Variant C selected**.
- **Tests — `turn-grouping.test.ts`:**
  - Add cases encoding the scenario matrix (close/no-close × idle/active × leading/inter-step/trailing × single/multiple messages), asserting reply detection and positional classification on the pure functions.
- **Out of scope:**
  - Any `tk close` flag or `tk`-code change for promotion — the close-time nudge plus re-output covers the speak-then-close case. Revisit a flag only if re-output proves too noisy in practice.
  - The no-steps "ungrouped work" rendering. `UngroupedWorkBlock.ts` and its `ChatPanel` wiring are left **as-is** (not enhanced, not deleted) so no-steps turns still render; a separate future effort replaces that experience. Do not apply the position-aware logic to it here.

## Open questions

- **Inter-step visual:** RESOLVED — Variant C (broken-thread, full-width prose block). Implementation must interrupt the thread with an opaque block + top/bottom hairlines without drawing any line across the text (the mock's first attempt had a white vertical mask bug, now fixed).
- **Settled-caption fallback:** when an unclosed-idle step has in-step narration but no promoted reply, do we surface that narration as a static caption, or prefer a fully bare node? Lean toward showing it, but confirm against the regenerated mocks.
- **Nudge mechanics:** which hook event (`PreToolUse` vs `PostToolUse` on the `tk close` Bash call) reads best, and how to robustly detect "text since the last tool call" from the transcript; plus whether frequent firing becomes noisy enough to want suppression (or a flag) later.
