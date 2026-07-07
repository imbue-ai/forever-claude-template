# Chat Scroll and Text-Selection Fixes

> Implementation plan derived from the verified diagnosis in
> `specs/chat-scroll-and-selection-bugs.md` (adversarially verified root causes,
> file:line evidence, and the detailed rationale behind every choice below).
> This plan is the actionable summary; the spec is the reference. All paths are
> relative to `apps/system_interface/frontend/src/`.

## Overview

- Three user-reported bugs in the chat transcript: text selection dies on scroll, selection dies while the agent streams output, and scroll position oscillates ("freaks out") when scrolling up through history or sitting at the bottom during streaming.
- Root causes are verified, not hypothesized: an unconditional `innerHTML` reset in `MarkdownContent.onupdate`, virtualization unmounting selection-bearing rows, a defective backfill prepend compensation that self-sustains a yank loop, uncompensated estimate-vs-measured height shifts (including a permission-row key/DOM-id mismatch), and a wheel-swallow race between the follow-mode bottom pin and async scroll event dispatch.
- Key decision: make the app the single owner of `scrollTop` (`overflow-anchor: none` plus row-key-anchored relative compensation) instead of fighting native scroll anchoring.
- Key decision: auto-follow never pauses while text is selected. Selection survives because scrolling never collapses a selection -- only DOM destruction does -- so we keep the selected rows mounted (window pinning) and their events held (eviction gate) while the view keeps chasing the tail.
- Key decision: fix in place with memoization and targeted hardening; no re-architecture, no new dependencies. `@tanstack/virtual-core` was evaluated and deferred; `content-visibility` occlusion and selection serialize/restore were rejected (see spec).
- Live-turn DOM identity churn (step 9 in the spec) is deliberately deferred until the core fixes are re-tested against the symptom.

## Expected behavior

- Selecting text anywhere in the transcript and scrolling (wheel, drag, scrollbar) keeps the selection intact, including through backfill paging.
- Selecting text and letting the agent stream keeps the selection intact while the view continues to auto-follow the tail; the selected text scrolls off-screen but stays selected, and Cmd+C copies it in full.
- Holding a drag (button down) over the transcript while output streams holds the view still for the duration of the drag; on release the view snaps back to the tail, selection intact. Follow itself never disengages from a selection.
- Scrolling up through a long transcript is smooth: no rhythmic downward yanks across prepends, no self-firing page loads, no oscillation.
- At the bottom during rapid streaming, one wheel-up tick reliably disengages follow; no swallowed input, no bounce.
- Staying pinned at the bottom through eviction and turn regrouping produces no visible jump or one-frame collapse.
- Dragging the scrollbar deep into unloaded history fires exactly one window load and lands at the top of the loaded content.
- Accepted degradations: a selection more than ~300 rows behind the live tail is dropped (bounded memory); a selection inside the live turn's still-restructuring prose can drop at regrouping moments until step 9 lands; far scrollbar-drag jumps still drop the selection.

## Implementation plan

- `markdown.ts` -- add `onbeforeupdate(vnode, old)` to `MarkdownContent` returning `vnode.attrs.content !== old.attrs.content`; keep the existing expanded-state save/restore for the changed path.
- `views/message-renderers.ts` -- `renderPermissionItem` gains an optional `domId` parameter (default `event.event_id`) used as the root `id`.
- `views/conversation-rows.ts` -- pass the row key `perm-<event_id>` as `domId` for top-level permission rows, restoring the `id === key` invariant for every row type.
- `style.css` -- add `overflow-anchor: none;` to `.app-content` (covers both scrollers). Lands only together with the compensation below.
- `views/ChatPanel.ts` --
  - delete `scrollHeightBeforePrepend` / `prependCompensationPending` and the delta block; replace with a row-key scroll anchor (capture on scroll events; apply a relative correction in `applyScrollPosition` only while `userScrolledUp`; re-capture and skip the write when the anchor key vanished; reset on agent switch and after the post-jump pin).
  - follow branch: honor pending user wheel-up before pinning, guarded by `Math.min(scrollTop, maxScroll)` so shrink-clamps are not read as user intent.
  - `maybePage`: replace the global fraction->index mapping with phantom-region geometry (exact inverse of the renderer's `ESTIMATED_EVENT_HEIGHT_PX` math); never jump while the viewport is over loaded rows; keep hysteresis and in-flight guards.
  - selection pinning: memoize a key->index map alongside `cachedRows`; resolve `document.getSelection()` endpoints (both anchor and focus) to row indices scoped to this view's `.message-list`; pass `pinnedRange` to the window math; drop the pin when the gap to the window exceeds ~300 rows.
  - gate `evictOldEvents` while an active selection resolves to held rows.
  - mid-drag pin deferral: `pointerdown` on the scroll container sets a flag, `pointerup`/`pointercancel` on `window` clears it; the follow branch skips `scrollToBottom` while set, without touching `userScrolledUp` or writing `scrollTop`.
- `views/SubagentView.ts` -- mirror the anchor compensation, follow hardening, selection pinning, and mid-drag deferral (simpler: no phantom regions).
- `models/scrollFollow.ts` + `models/scrollFollow.test.ts` -- extend `FollowStateInput` with an `isClamp` bit (preserve prior state on clamp instead of re-arming via `isNearBottom`); add the pure active-selection predicate; unit tests for both.
- `models/virtualWindow.ts` + `models/virtualWindow.test.ts` -- add optional `pinnedRange` input (expand window, clamp to `[0, count)` inside the function, recompute pads); replace the past-the-end "render only the last row" branch with a backward fill covering `viewportHeight + 2*overscanPx`; update the pinned single-row test and add pinned-range cases.
- `views/row-measurement.ts` -- no changes.

## Implementation phases

- Phase 1 -- stop rewriting selected DOM (spec steps 1-2): `MarkdownContent` memoization and the permission-row id fix. Smallest diff, fixes the dominant selection killer on both scroll and stream; fully shippable alone.
- Phase 2 -- single scrollTop owner (spec steps 3-4): `overflow-anchor: none` + row-key anchor compensation + follow hardening, ChatPanel and SubagentView together. Fixes the yank loop and the at-bottom wheel fighting.
- Phase 3 -- selection survives virtualization and follow (spec steps 5-6): `pinnedRange` in the window math, selection resolution, eviction gate, ~300-row cap, mid-drag deferral. Completes the "text stays selected under auto-follow" requirement.
- Phase 4 -- boundary hardening (spec steps 7-8): geometry-consistent jump trigger and the past-the-end backward fill. Removes the rare-but-violent full-teardown events.
- Phase 5 (deferred, re-scope after testing) -- live-turn DOM identity stabilization (spec step 9), only if selection loss at step-creation moments is still a complaint after phases 1-4.

## Testing strategy

- Unit tests (vitest, `pnpm test` in `apps/system_interface/frontend/`):
  - `scrollFollow.test.ts`: clamp preserves prior state; wheel-up still disengages; re-arm only at true tail; selection predicate truth table.
  - `virtualWindow.test.ts`: pinned-below / pinned-above / pinned-inside / pinned + past-the-end / out-of-range clamping; backward-fill coverage and pad invariants (pads + rendered == total).
  - A `MarkdownContent` component test asserting `onupdate` does not touch the DOM when content is unchanged (mount, snapshot a text node reference, redraw, assert same node).
- Lint/format gates: `pnpm lint` and the existing `lint-and-format.test.ts`.
- Empirical pre-check for phase 2: confirm with two `console.log`s that the scroll container's `onupdate` runs before freshly-prepended descendants' `oncreate` (the spec's hook-ordering claim) before relying on live `offsetTop` reads.
- Manual verification: run the full checklist in `specs/chat-scroll-and-selection-bugs.md` section 4 against a >2000-event transcript with step turns and a permission card, plus a live streaming agent. Browser-interaction checks stay manual (not crystallized into flaky DOM tests); the behaviors that can be expressed as pure functions are crystallized in the unit tests above.
- Regression sweep: normal follow engage/disengage, backfill paging, offset jumps, subagent view parity.

## Open questions

- Mid-drag pin deferral: recommended and included (making a selection mid-stream is nearly impossible without it), but severable if even a momentary hold is unwanted.
- Cap value: is ~300 rows the right pin-abandonment threshold, and should it be shared between ChatPanel and SubagentView as one constant?
- Native drag-to-edge auto-scroll during a selection drag decreases `scrollTop` and will disengage follow like any upward scroll. Consistent with wheel semantics -- acceptable, or should drag-scroll be exempted?
- Step 4.4 (`e.redraw = false` scroll-frame suppression) is a disputed perf optimization; skip unless scroll cost still shows after phase 1?
- Spec step 9 (live-turn identity): re-scope after phases 1-4, or drop entirely if the residual is tolerable?
