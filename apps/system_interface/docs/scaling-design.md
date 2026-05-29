# PR 4: Scaling architecture -- design report

Status: re-assessed after stacking on `gabriel/correctness-ob`. Awaiting
design confirmation. No implementation code written.

---

## 0. Re-assessment after stacking on `gabriel/correctness-ob`

This branch is now merged on top of `gabriel/correctness-ob` (the PR 2
territory I had flagged as a dependency). That branch already reworked the
**backend** scaling and changes the picture materially. Sections 1-6 below are
the original pre-merge analysis against plain `main`; read them through the
lens of this section.

### What correctness-ob already fixed

- **Finding (a) -- whole-file re-read.** SOLVED on disk.
  `SessionFileState` now holds an **append-only in-memory parsed-events cache**
  (`session_watcher.py:99-113`); `_ensure_cache_current` (`:218-280`) reads
  only the bytes appended past `byte_offset_consumed` and parses just that
  tail (with correct handling of partial trailing lines and truncation/rewrite
  via the dedup-set discard at `:242-246`). A file is fully parsed once, then
  only its growing tail. Disk I/O per `get_all_events` call is now O(delta),
  not O(N).
- **Finding (c) -- initial response returns everything.** SOLVED.
  `_get_events` now returns `watcher.get_all_events()[-limit:]`
  (`server.py:301`), with `_DEFAULT_TAIL_COUNT = 50` and a guard against
  non-positive limits (`:289-292`). The payload is bounded.
- **Truncation / atomic-rewrite correctness**, partial-line safety, and
  SSE-delta buffering during the initial snapshot fetch
  (`loadSnapshotWithStream` in `StreamingMessage.ts`, wired into
  `ChatPanel.loadAgent`) are all handled. Reconnect backoff and safe JSON
  parsing on the WS/SSE handlers too.

### What correctness-ob did NOT fix (still O(N) -- the remaining work)

- **(a-residual) `get_all_events` is still O(N) CPU + memory per call.**
  Even with incremental disk reads, every call does
  `all_events.extend(state.events)` over the full cache, then
  `all_events.sort(...)` (O(N log N)), then `_enrich_subagent_metadata`
  (O(N)) -- `session_watcher.py:184-191`. And the cache itself
  (`state.events`) now holds **every event in memory for the agent's
  lifetime, by design** -- so backend memory is O(N) (this supersedes my old
  finding (d): it is no longer an accidental leak but a deliberate cache, and
  is the new ceiling).
- **(b) `get_backfill_events` is still O(N) per page -> O(N^2) paging.**
  `session_watcher.py:194-210` still calls `get_all_events()` (now O(N) CPU,
  not O(N) disk) and linear-scans for `before_event_id`. No bounded range
  access.
- **(f) No frontend virtualization.** `ChatPanel.renderMessages` and
  `SubagentView` still render every message to the DOM. Untouched.
- **(g) Eager backfill drain loop still present.** `runBackfillLoop`
  (`ChatPanel.ts:231-257`, `startBackfill` at `:260`) still loops
  `while (!isBackfillComplete(agentId))`, pulling the *entire* transcript to
  the client on open -- defeating the new server-side tail cap. Untouched.
- **(e/h) `Response.ts` untouched.** `eventsByAgent` still holds the whole
  transcript; `appendEvents`/`prependEvents` still rebuild an O(N) id Set per
  call -> O(N^2) over a session.

### How this re-scopes PR 4

The sidecar **on-disk `.idx` file (old sub-PR 4a) is now largely unnecessary**
and I recommend dropping it. correctness-ob's in-memory append-only cache
already provides incremental parsing; a disk index would only add value for
(i) avoiding a one-time re-parse on process restart and (ii) bounding backend
memory below O(N). Neither is the user-facing problem, and (ii) directly
contradicts correctness-ob's deliberate "hold everything in memory" choice.
Adding a disk index now would be a large, redundant change fighting the branch
we are stacking on.

**Revised plan -- PR 4 becomes primarily a frontend effort plus one small
backend bound:**

- **4a (backend, small): bounded backfill over the in-memory cache.** Give
  `SessionFileState` (or the watcher) an `event_id -> cache index` map, so
  `get_backfill_events` returns a bounded slice of the already-parsed,
  already-cached `state.events` without re-sorting or re-scanning the whole
  transcript. This kills (b) cheaply, reusing correctness-ob's cache instead
  of a new file format. Also add a `has_more` flag to the events response.
  (Optional: memoize the merged+sorted+enriched view so `get_all_events`
  isn't re-sorting O(N) per call -- only re-sort when the cache grew.)
- **4b (frontend, the bulk): kill the eager loop + virtualize.**
  - Replace `runBackfillLoop`'s drain-to-completion with a **scroll-triggered
    single-page** fetch (finding g). This is the highest-leverage fix:
    without it, no amount of server-side capping helps.
  - **Minimal in-house windowed list** (finding f, i) -- recommendation
    unchanged from section 2.4; rationale (dynamic/collapsible heights) still
    holds.
  - **Persistent per-agent id Set** in `Response.ts` (finding h).
- **4c (frontend, optional): client-side eviction** of far-offscreen events
  from `eventsByAgent` (finding e), re-fetch on scroll-back via 4a. Defer
  unless profiling shows JS heap pressure.

A tiny **4b-pre** (eager-loop fix + dedup fix, ~Response.ts + ChatPanel only)
is still worth shipping first as an independent low-risk win.

### Dependency note now resolved

The PR 2 cursor-identity question from section 4 is effectively answered by
stacking: correctness-ob keeps `event_id` (`<uuid>-<suffix>`) as the stable
identity and the events-response shape, so 4a can safely key its backfill map
on `event_id`. The agent-destroy/watcher-teardown coordination still applies.

### Revised open questions

1. Agree to **drop the on-disk sidecar index** in favor of bounded access to
   correctness-ob's in-memory cache? (My recommendation: yes.)
2. Is backend memory at O(N) (the cache holding all events) acceptable as the
   standing ceiling, deferring any disk-backed/evicting cache to future work?
   (correctness-ob already committed to this; PR 4 would not change it.)
3. Minimal in-house virtualization (recommended) vs. `@tanstack/virtual`?
4. Client-side eviction (4c): in scope now, or deferred pending profiling?
5. Ship the eager-loop + dedup fix (4b-pre) as an independent first PR?

### CONFIRMED DECISIONS (user, 2026-05-28)

1. **Drop the on-disk sidecar `.idx` file.** Confirmed.
2. **Bound backend memory with an evicting cache** -- do NOT leave it at O(N).
   This *reverses* correctness-ob's "hold every parsed event forever" choice
   and is now in scope for PR 4.
3. **In-house virtualization.** Confirmed (no `@tanstack/virtual`).
4. **Client-side eviction is in scope** for this work.
5. **Do not split out a 4b-pre.** The eager-loop + dedup fixes fold into the
   main frontend PR.

### Reconciling decisions 1 + 2: the consequence to sign off on

Decisions 1 and 2 are in tension and the resolution determines the backend
shape. If the parsed-event cache **evicts** old events (2) to bound memory,
then backfilling history that has been evicted must re-read it **from the
JSONL on disk**. With NO index of any kind, locating "the N events before
`event_id` X" on disk means re-scanning the file -> O(N) disk per backfill of
old data -> O(N^2) paging. That just moves correctness-ob's problem back onto
disk.

The resolution: **drop the on-disk index file (1), but keep a compact
*in-memory* locator index (2).** Concretely, split today's monolithic
`state.events` (full parsed bodies) into two tiers per session file:

- **Locator tier (never evicted, tiny):** an ordered list of
  `(event_id, timestamp, byte_offset, byte_len)` built incrementally as the
  watcher tails -- the same loop that already advances `byte_offset_consumed`.
  No message text, no tool output. ~tens of bytes/event; ~16 MB for 1M events.
  This is O(N)-count but small, and lives only in memory (no `.idx` file).
- **Body tier (bounded, evicting):** an LRU window of fully-parsed events
  (the heavy `text`/`output`/`tool_calls` payloads). Capacity is a fixed
  number of events (e.g. a few thousand). On a miss, seek to the locator's
  `byte_offset`, read `byte_len`, parse, and populate.

`get_tail(limit)` and `get_backfill(before_event_id, limit)` both become:
locate via the in-memory index (O(log N) or O(1)), ensure those `limit` bodies
are resident (bounded disk read on miss), return. No full re-sort, no
re-scan, no full-file read. Backend memory is then bounded by the body-tier
cap plus the compact locator index.

**This is the one point I want explicit sign-off on:** "drop the on-disk
index" + "bounded memory" together still implies a small *in-memory* locator
index (O(N)-count but bodies-free). If you truly want O(1) backend memory
(not even the locator list), the only way is an on-disk index, which decision
1 rules out. I read your answers as: in-memory locator index is fine, just no
sidecar file. Please confirm.

(Sparse variant, if even the locator list feels too big: checkpoint a
`byte_offset` every K events instead of every event -> O(N/K) memory; backfill
seeks to the nearest checkpoint and parses forward <= K events. Noted as an
implementation tuning knob, not a separate decision.)

### Revised sub-PR sequence (post-confirmation)

- **4a (backend): two-tier evicting cache + bounded tail/backfill.** DONE.
  `SessionFileState` now holds a compact `EventLocator` index (a `tuple`
  subclass, `__slots__`, never evicted); parsed bodies live in a bounded LRU
  (`_DEFAULT_BODY_CACHE_CAPACITY`, injectable) keyed by `event_id`, re-read from
  disk via the locator byte range on a miss. `get_tail_events` and
  `get_backfill_events` locate via `_locator_ref_by_id` (O(1)) and resolve at
  most `limit` bodies (O(limit) disk, independent of N). `_get_events` returns
  the bounded tail plus a `has_more` flag. Tests added: oracle-equivalence for
  tail/backfill across resumed files and multi-event lines, correctness of
  backfill over evicted history, disk-reads-bounded-independent-of-N, and
  body-cache-capacity-respected-while-paging.
- **4b (frontend): scroll-triggered backfill + in-house virtualization +
  persistent dedup Set.** DONE. The eager `runBackfillLoop` is gone; history
  pages in one viewport at a time when the user scrolls near the top and the
  server reports `has_more`, with scroll-position compensation on prepend. The
  message list is virtualized via a pure, unit-tested `computeVisibleWindow`
  (`virtualWindow.ts`): only the viewport + overscan rows mount to the DOM,
  heights measured from the DOM with per-type estimates and top/bottom spacers
  for the rest, integrated with scroll-to-bottom / `userScrolledUp`.
  `Response.ts` keeps a persistent per-agent id Set so append/prepend dedup is
  O(1) per event. Applied to both `ChatPanel` and `SubagentView`.
- **4c (frontend): client-side eviction** of far-offscreen events from
  `eventsByAgent`. DONE. `evictOldEvents` trims the oldest events past a cap
  (only while following the live tail, so a scrolled-up reader is never
  disrupted) and flags `has_more` so scroll-up re-fetches via 4a's bounded
  backfill. Bounds client JS memory for an arbitrarily long live conversation.

Acceptance criteria to encode as tests:
- Backend: backfill of *evicted* history does a bounded number of disk-read
  bytes independent of N (a test that fails on an O(N) re-scan); tail +
  backfill correctness vs. a brute-force oracle over a synthetic 50k-100k
  event transcript spanning multiple (resumed) session files and
  multi-event lines; body-tier cap is respected (resident count stays
  bounded while paging across the whole history).
- Frontend: only viewport (+overscan) messages are in the DOM for a large
  transcript; scroll-up triggers exactly one backfill page (not a drain);
  scroll position preserved across prepend; dedup is O(1) amortized per
  event; client memory bounded after scrolling through and back.

---

## 1. Confirmed problem: O(total-transcript) stages

> Note: section 1 is the original analysis against plain `main`, before the
> correctness-ob merge. See section 0 for which of these are now fixed.

Data path traced end to end. Every stage below is O(N) or worse in the total
transcript length N (events), and several are hit repeatedly.

### Backend

**(a) `get_all_events` re-reads + re-parses the whole file.**
`session_watcher.py:120-138`: `content = state.file_path.read_text()` then
`splitlines()` then `parse_session_lines(lines, ...)` over *every* line, for
*every* main session file, on *every* call. It then `all_events.sort(...)`
(O(N log N)) and `_enrich_subagent_metadata(all_events)` (two more O(N)
passes). Nothing is cached -- a 50 MB JSONL is fully read and parsed on each
invocation. Confirmed.

**(b) `get_backfill_events` re-reads the whole file per page.**
`session_watcher.py:146`: `all_events = self.get_all_events(session_id=...)`,
then a linear scan to find `before_event_id` and a slice. Each backfill page
therefore costs a full file read+parse+sort. Paging back through a long
transcript is O(N) per page x O(N/limit) pages = **O(N^2)**. Confirmed.

**(c) `_get_events` with no `before` returns the entire transcript in one
response.** `server.py:290-294`: the `else` branch calls
`watcher.get_all_events()` and `server.py:296` serializes the full list as one
`JSONResponse`. The `_DEFAULT_TAIL_COUNT = 50` constant exists but is only
applied to the *backfill* path, never to the initial load. Confirmed. Same for
`_get_subagent_events` (`server.py:361`).

**(d) Unbounded watcher memory.** `_existing_event_ids: set[str]` and
`_tool_name_by_call_id: dict` (`session_watcher.py:74-75`) grow by one entry
per event/tool-call for the agent's lifetime and are never trimmed. Not in the
prompt's list but it is a real O(N) memory leak for an "infinite" session.

(The SSE replay buffer is *not* a leak: `server.py:149-154` broadcasts session
events with `BufferBehavior.IGNORE`, so `AgentEventQueues._event_buffers`
stays empty for them.)

### Frontend

**(e) Whole transcript held in memory.** `Response.ts:71` `eventsByAgent` is a
`Record<agentId, TranscriptEvent[]>`; `fetchEvents` (`Response.ts:127`) does
`eventsByAgent[agentId] = result.events` -- the entire transcript.

**(f) Every message rendered to the DOM, no virtualization.**
`ChatPanel.ts:374-392` `renderMessages` loops all `events`, builds a
`messageNodes` vnode per user/assistant message, and renders them all into one
`.message-list` div. DOM node count is O(N). `SubagentView.ts:147-162` does the
same. Confirmed.

**(g) Backfill is an eager full-drain loop, not on-demand.** This is the worst
frontend finding and is *not* in the prompt's list. `ChatPanel.startBackfill`
-> `runBackfillLoop` (`ChatPanel.ts:230-265`) loops `while
(!isBackfillComplete(agentId))`, calling `fetchBackfillEvents` back to back
until the whole history is pulled. It is **not** gated on scroll position. So
even though the initial load *could* be a tail, the client immediately pages
the entire transcript into `eventsByAgent` and the DOM anyway. Tail-first
loading is currently defeated by this loop. Any virtualization work is moot
unless this loop becomes scroll-triggered.

**(h) O(N) dedup per delivered event.** `appendEvents` / `prependEvents`
(`Response.ts:95-115`) rebuild `new Set(existing.map(e => e.event_id))` on
every call. Each live SSE event (`StreamingMessage.ts:46`) triggers an O(N)
scan -> O(N^2) over a session. Plus each append does `[...existing,
...deduped]`, copying the whole array.

**(i) MarkdownContent re-render.** `markdown.ts:77-93` sets
`element.innerHTML = renderMarkdown(...)` + `DOMPurify.sanitize` on mount and
on update. `StableAssistantMessage.onbeforeupdate` (`message-renderers.ts:113`)
correctly memoizes so it does *not* re-parse unchanged messages on redraw --
good. But the *initial* mount still parses+sanitizes all N messages, and all N
parsed subtrees stay resident. This is a constant the windowing in (f) must
also cover (only mount MarkdownContent for visible messages).

### Summary table

| Stage | File | Cost | Frequency |
|---|---|---|---|
| get_all_events read+parse+sort | session_watcher.py:120 | O(N log N) | every load |
| get_backfill_events | session_watcher.py:146 | O(N) per page | every page -> O(N^2) |
| _get_events full response | server.py:294 | O(N) payload | every load |
| watcher id/tool sets | session_watcher.py:74 | O(N) memory | grows forever |
| eventsByAgent | Response.ts:71 | O(N) memory | -- |
| renderMessages -> DOM | ChatPanel.ts:374 | O(N) DOM nodes | -- |
| eager backfill loop | ChatPanel.ts:236 | O(N) fetch + O(N^2) dedup | once per open |
| append/prepend dedup | Response.ts:98 | O(N) per event | every SSE event |

## 2. Proposed design

The proposal (sidecar index, cursor backfill, virtualization, eviction) is
sound in direction. Refinements below; the one substantive disagreement is
about *what* the index keys and how virtualization is built.

### 2.1 Logical model (important nuance)

The proposed "`event_id -> byte_offset`, one entry per event" is not quite
right because of two facts:

1. **One JSONL line yields multiple events.** A `user` line with tool_result
   blocks produces a `user_message` event *and* one `tool_result` event per
   block (`session_parser.py:202-280`). So multiple event_ids map to the *same*
   byte offset.
2. **One agent transcript spans multiple files.** Resumed sessions are listed
   in `claude_session_id_history`; `get_all_events` reads *all* main session
   files and merge-sorts by timestamp. Backfill pages over this *merged*
   sequence, not a single file.

So the index is **per-file**, recording one entry per *event* but with a
line locator, and the merged transcript is the per-file indexes ordered by
file. Resumed sessions do not overlap in time (the old session is finished
before the new one starts), so the merged order is simply the concatenation
of files sorted by first-timestamp -- no cross-file interleaving, no bisect
needed. (Fallback if that assumption is ever violated: detect overlapping
`[first_ts, last_ts]` ranges at index-load time and fall back to the current
read-all path for that agent, logging a warning. I do not expect this to fire.)

### 2.2 Sidecar index (sub-PR 4a)

Per session file `<name>.jsonl`, a sibling `<name>.jsonl.idx`:

- Append-only, one line per *event*, written by the watcher as it tails:
  `{"event_id": "...", "type": "user_message", "ts": "...", "line_off": <int>, "line_len": <int>}`
  (`line_off`/`line_len` locate the source JSONL line; `ts` lets us merge-order
  files and answer tail queries without opening the JSONL).
- A header/first line records `{"idx_version": 1, "covers_bytes": <int>}` or,
  simpler, `covers_bytes` is derived from the last entry's `line_off+line_len`.
- **Lazy build / backward compat:** on first access, if `.idx` is missing,
  scan the JSONL once (the unavoidable one-time O(N)) and write it. If `.idx`
  exists but `covers_bytes < session_file_size`, scan only the tail remainder
  and append. If `covers_bytes > size` (file shrank/rewritten -- not expected
  for Claude session files, which are append-only), discard and rebuild.
- The watcher's existing incremental tail (`_poll_for_changes`,
  `session_watcher.py:348-393`) already reads new bytes from `byte_offset` and
  parses them; it gains an index-append alongside the existing
  `_on_events` callback. Index maintenance is free-riding on work already done.

In-memory representation per watcher: an ordered `list[IndexEntry]` per file
plus a `dict[event_id, (file_id, list_pos)]`. This is O(N) *count* but tiny
per entry (~3 ints + 2 short strings). For a 1M-event transcript that is on
the order of ~100 MB -- large but bounded and far better than re-reading a
multi-GB file. If that ceiling is unacceptable we can later switch to a
fixed-width on-disk index with binary search (future improvement, noted, not
in scope). I recommend accepting the in-memory index for now.

### 2.3 Cursor-based backfill + bounded tail (sub-PR 4b)

- `get_backfill_events(before_event_id, limit)`: look up the index position of
  `before_event_id`, take the `limit` preceding index entries, collect their
  *distinct* `(file, line_off, line_len)` triples (<= `limit` of them),
  `seek`+`read` exactly those byte ranges, parse, and return the events.
  O(limit), no full read.
- New `get_tail_events(limit)`: take the last `limit` index entries, same
  bounded read. `_get_events` with no `before` calls this instead of
  `get_all_events`. The response gains `{"events": [...], "has_more": bool}`
  so the client knows whether older history exists.
- `get_all_events` is kept only as the lazy index-build fallback and is no
  longer on any request hot path. Subagent loads
  (`_get_subagent_events`) use the same tail/backfill mechanism against the
  subagent's own file index.
- **API contract change** (additive, backward compatible for old clients that
  ignore `has_more`):
  - `GET /api/agents/{id}/events` -> now returns the tail (last N) +
    `has_more`, instead of everything.
  - `GET /api/agents/{id}/events?before=<event_id>&limit=<n>` -> unchanged
    shape, now O(limit).
  - Subagent endpoints mirror this.

### 2.4 Frontend: on-demand backfill + virtualization (sub-PR 4c)

This is the largest piece and depends on 4b.

1. **Kill the eager loop (finding g).** Replace `runBackfillLoop`'s
   drain-to-completion with a scroll-triggered single-page fetch: when the
   scroll container nears the top and `has_more`, fetch one page and prepend,
   preserving scroll offset (anchor on the first previously-visible event's
   DOM node). Keep the existing stalled-retry/backoff logic for the "page
   came back empty but server says more" race.
2. **Windowed rendering.** Recommendation: **build a minimal windowed list,
   do not pull in `@tanstack/virtual`.** Reasoning: our message heights are
   highly variable and *dynamic* -- tool-call blocks expand/collapse on click
   (`message-renderers.ts:186`), markdown height is unknown until rendered.
   Virtualization libs assume a measure/estimate model that we would have to
   feed anyway, and integrating one with Mithril's redraw lifecycle plus the
   existing `oncreate`/`onupdate` scroll logic in `ChatPanel` is comparable
   effort to a purpose-built window. The minimal window:
   - Keep a `Map<event_id, measuredHeight>`, populated via `ResizeObserver`
     on mounted message nodes; unmeasured messages use a per-type estimate.
   - Render only messages whose estimated cumulative offset intersects
     `[scrollTop - overscan, scrollTop + clientHeight + overscan]`, with a
     top spacer and bottom spacer div sized by the summed estimated heights of
     the off-window messages.
   - `MarkdownContent` is only ever mounted for in-window messages, so
     finding (i) is covered.
   I will present this as a decision point -- if you would rather take the
   library, the integration is feasible, just heavier.
3. **Integration with existing scroll behavior.** The current
   `userScrolledUp` / `isNearBottom` / `applyScrollPosition` logic
   (`ChatPanel.ts:51-289`) must keep working: scroll-to-bottom on new events
   when not scrolled up, preserve position on backfill prepend. The window's
   spacer math must be the single source of truth for `scrollHeight` so these
   checks stay correct.
4. **Fix O(N^2) dedup (finding h).** Maintain a persistent
   `Set<event_id>` per agent in `Response.ts` instead of rebuilding it in
   every `appendEvents`/`prependEvents`.

### 2.5 Client-side eviction (sub-PR 4d, optional/last)

With windowing, `eventsByAgent` still holds all N event *objects* (not DOM).
Event objects are bounded per item (tool output truncated to 2000 chars,
`session_parser.py:16`), so this is far less urgent than DOM. Propose 4d as a
final, separable PR: evict events outside a generous window from
`eventsByAgent`, re-fetch via the 4b backfill endpoint on scroll-back. This
turns the client into a true sliding window. Recommend doing it only if
profiling shows JS heap pressure; otherwise defer.

## 3. Recommended PR sequence

1. **4a -- sidecar index** (backend only, no API change). Watcher builds and
   maintains `.idx`; `get_all_events` internally backed by index where
   possible. Shippable and testable in isolation.
2. **4b -- cursor backfill + bounded tail** (API change, additive). Depends
   on 4a.
3. **4c -- frontend on-demand backfill + virtualization.** Depends on 4b.
   Largest PR; the eager-loop fix (g) and dedup fix (h) could be split out as
   a tiny **4c-pre** that is independently valuable and low-risk.
4. **4d -- client eviction.** Optional, depends on 4c.

## 4. Dependency on PR 2

PR 2 is changing the SSE/event-queue protocol and agent-destroy eviction.
Concrete coupling points to reconcile before/while implementing:

- **Cursor identity.** This design uses `event_id` (the
  `<uuid>-<suffix>` string from `session_parser._make_event_id`) as the
  backfill cursor and index key. If PR 2 introduces an opaque/sequential
  cursor or changes `event_id` format, 4a/4b must key the index on whatever
  PR 2 settles on. **Action: confirm PR 2 keeps `event_id` stable, or agree a
  shared cursor type.**
- **Buffer behavior.** 4b assumes session events stay `BufferBehavior.IGNORE`
  (recoverable from disk via the index) rather than living in the in-memory
  replay buffer. If PR 2 reworks `AgentEventQueues` buffering, that assumption
  must hold or the index becomes the sole backfill source by agreement.
- **Agent-destroy eviction.** When PR 2 evicts a destroyed agent, the watcher
  and its in-memory index for that agent should be torn down too
  (`_stop_all_watchers` already exists; eviction needs to call per-agent
  cleanup). Coordinate so neither PR leaks watchers.

## 5. Testing plan (to execute after design confirmation)

- Backend unit tests with synthetic large transcripts (10k-100k events):
  index build correctness, incremental append, lazy build of a pre-existing
  un-indexed file, multi-file (resumed session) merge order, multi-event
  lines, `.idx` staleness/rebuild, tail and backfill correctness vs. a
  brute-force `get_all_events` oracle.
- Backend performance assertion: backfill page cost does not scale with N
  (e.g. bounded read-byte count), expressed as a test that would fail on the
  current O(N) implementation.
- Frontend `vitest`: windowing math (visible-range computation, spacer
  sizing), on-demand backfill trigger, scroll-position preservation on
  prepend, persistent-set dedup.
- Manual verification per CLAUDE.md: drive the app with a synthetic long
  transcript, confirm responsiveness and correct scroll behavior.

## 6. Open questions for confirmation

1. Virtualization: minimal in-house window (recommended) vs. `@tanstack/virtual`?
2. Accept O(N)-count in-memory index now, with fixed-width on-disk index as a
   noted future improvement -- ok?
3. Is sub-PR 4d (client eviction) in scope for this work, or explicitly
   deferred pending profiling?
4. PR 2 cursor identity -- can we rely on `event_id` staying the stable
   backfill cursor?
5. Should the tiny eager-loop + dedup fix (4c-pre) be pulled out and shipped
   first as an independent low-risk improvement?
