# PR 3 Investigation Report — Frontend Refactor

Investigation of the audit-flagged issues for PR 3 (frontend-only): moving side
effects out of Mithril render functions, memoization, and dead-code cleanup.

Status: investigation complete, **awaiting plan confirmation before
implementation**. No production code changes yet.

## Re-assessment after stacking on `gabriel/correctness-ob`

This branch is now merged on top of `gabriel/correctness-ob`, which reworked
the SSE/WS robustness layer (per-agent reconnect backoff, `parseJsonMessage`
guard, `loadSnapshotWithStream` folding the stream-open into the snapshot
fetch). Impact on the PR 3 plan:

- None of the PR 3 items were fixed by correctness-ob. The side effects are
  still in `view()`; `MessageInput` and `markdown.ts` are untouched.
- **Item A now routes the initial stream-open through
  `loadSnapshotWithStream`** (called from `loadAgent`), while
  `manageStreamConnection` still calls `connectToStream` from the view path.
  `connectToStream` is idempotent, so this is safe, but the lifecycle refactor
  must preserve both call sites (initial load + not-found disconnect). There is
  also a simplification opportunity: on initial load the `manageStreamConnection`
  connect is redundant with `loadSnapshotWithStream`.
- **Item E (StreamingMessage shims) still holds.** The 8 no-op shims
  (`getStreamingMessage` ... `markStreamingError`) and the `StreamingMessage`
  interface remain entirely unreferenced after the merge; the new
  `StreamingMessage.test.ts` only exercises `loadSnapshotWithStream`. The file
  is no longer "mostly shims," though -- it is now substantial live code plus a
  dead shim block at the bottom. Delete only that block.
- **New low-risk bolt-on (H4):** correctness-ob added `ws-json.ts`
  `parseJsonMessage` and applied it to the SSE streams and `SubagentView`, but
  the proto-agent log WebSocket in `ChatPanel.connectLogWs` still uses a raw
  `JSON.parse(event.data)` that would throw out of the `onmessage` handler on a
  malformed frame. Route it through `parseJsonMessage` for consistency.
- Items B, C, D, F, G, H1 are unchanged by the merge.

## A. Side effects fired from inside `view()` — CONFIRMED (proposed fix partially misguided)

`ChatPanel.ts` `view()` (line ~419) calls `renderMessages(agentId)`, which
synchronously triggers `connectLogWs` (`:167`), `ensureAgentLoaded` (`:314`),
`manageStreamConnection` (`:315`), `fetchScreenCapture` (`:318`, redraws), and
`startBackfill` (`:365`, loops with `m.redraw()` at `:239`).

The "with explicit prev-attrs comparison" suggestion is the wrong mechanism:
`ChatPanel` is mounted by `createMithrilRenderer` with a fixed `attrs` object,
so `agentId` never changes for a panel instance. The side-effect triggers are
external store-state transitions (proto-agent list, `notFoundAgentIds`,
`loading`). Fix: `oninit` for first-time setup; `onupdate` re-evaluating the
same branch conditions `renderMessages` uses; keep the existing guards.

## B. `dockview.layout()` on every redraw — CONFIRMED, proposed fix is a MISFIRE

`DockviewWorkspace.ts:981-991` schedules `dockview.layout()` on every redraw.
dockview-core already uses an internal `ResizeObserver` by default
(`disableAutoResizing` defaults to `false`). Fix: delete the `onupdate` handler
entirely; do not add another `ResizeObserver`.

## C. `toolResults` Map rebuilt every redraw — CONFIRMED

`ChatPanel.ts:367-372` and `SubagentView.ts:140-145` rebuild a `Map` over all
events each redraw. Array identity is a sound memo key (`appendEvents` /
`prependEvents` reassign by reference only on change).

## D. `MarkdownContent` re-parses every `onupdate` — CONFIRMED

`markdown.ts:83-89` runs `marked.parse` + `DOMPurify.sanitize` unconditionally.
Cache last `content` per DOM element (component is a shared singleton) and skip
when unchanged.

## E. Dead / shim code — PARTIALLY CONFIRMED

- `ProtoAgentLogView.ts` — fully dead, safe to delete.
- `Response.ts` — only `getResponsesForConversation`, `getLastResponseModel`,
  `appendSyntheticResponse`, `fetchResponses`, `ConversationNotFoundError` are
  unreferenced. `getAllResponses` / `insertResponseItem` / `ResponseItem` are
  live (plugin API).
- `StreamingMessage.ts:99-117` shims + `StreamingMessage` interface — all dead.
- `Conversation.ts` — `Agent`, `getAgents`, `getAgentsLoaded`,
  `getLoadingError`, `fetchAgents`, `fetchConversations`, `toAgent` unreferenced;
  `getConversations` and `Conversation` type are live.
- Backend `_refresh_agents` (test-only) and `_known_session_ids` (write-only) —
  out of scope for this frontend-only PR; noted only.

## F. `alert()` for destroy errors — CONFIRMED (more involved than stated)

`DockviewWorkspace.ts:953,957`. The dialog is unmounted before `executeDestroy`
runs, so surfacing the error inline requires keeping `DestroyConfirmDialog`
mounted with a destroying/error state.

## G. Unbounded `logLines` — CONFIRMED

`ChatPanel.ts:81,139,187`. Cap to the last N lines.

## H. Bolt-ons

- H1 — `MessageInput.ts:53-57` swallows send errors after clearing the text:
  CONFIRMED; surface a failure state, consider restoring the text.
- H2 — `m.mount(element, null)` EventSource leak: NOT CONFIRMED; unmount is
  synchronous and `onremove` closes the stream; reconnect timer is guarded.
- H3 — `showCustomUrlDialog` raw `innerHTML` / listeners: MISFIRE; the HTML is a
  static literal (no XSS) and listeners are GC'd with the removed subtree.
