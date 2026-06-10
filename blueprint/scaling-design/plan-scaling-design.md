# Scaling the transcript UI to arbitrarily long conversations

The `system_interface` chat view must stay responsive for conversations of
unbounded length. This document specifies the architecture that keeps every
stage of the path -- from the on-disk JSONL transcript, through the HTTP/SSE
layer, to the rendered DOM -- bounded in cost per interaction rather than
growing with the total transcript length `N`.

## 1. The problem: O(N) and O(N^2) stages

A conversation transcript is a set of append-only JSONL session files (a resumed
session starts a new file; one agent's transcript is the time-ordered
concatenation of its files). A single JSONL line can yield more than one event
(a `user` line with tool-result blocks produces a `user_message` event plus one
`tool_result` event per block). Left unbounded, the data path has several stages
whose cost scales with `N`:

- **Reading the transcript.** Re-reading and re-parsing whole session files on
  every request is O(N) disk + O(N log N) sort per call.
- **Backfill paging.** Locating "the events before cursor X" by scanning the
  full transcript is O(N) per page, so paging back through history is O(N^2).
- **Initial payload.** Returning the entire transcript in the first response is
  an O(N) payload regardless of how much the client can show.
- **Backend memory.** Holding every parsed event body in memory for the agent's
  lifetime is O(N) memory.
- **Client memory + DOM.** Holding every event client-side and rendering every
  message to the DOM is O(N) JS memory and O(N) DOM nodes.
- **Live-delivery dedup.** Rebuilding an id set on every appended event is O(N)
  per event, i.e. O(N^2) over a session.

The design below removes each of these.

## 2. Backend: a two-tier cache with bounded tail/backfill

The watcher (`session_watcher.py`) never holds every parsed event body in
memory. Each session file is represented by two tiers.

### 2.1 Locator tier (never evicted, bodyless)

`SessionFileState.locators` is an ordered list of `EventLocator`, one per event:
`(event_id, timestamp, byte_offset, byte_len)`, where the byte range addresses
the source JSONL *line* (several locators can share a range when a line yields
multiple events). `EventLocator` is a `tuple` subclass with `__slots__ = ()`, so
each entry is as small as a plain 4-tuple -- there is one per event for the whole
transcript, so the per-event footprint is the point. The locator index is built
incrementally as the file is tailed and is never evicted. It is O(N) in *count*
but carries no message/tool bodies (tens of bytes/event), and lives only in
memory -- there is no on-disk index sidecar.

A companion `_locator_ref_by_id: dict[event_id, (SessionFileState, index)]` gives
O(1) lookup of any event's position.

### 2.2 Body tier (bounded LRU)

`_body_cache` is an `OrderedDict[event_id, event]` LRU of fully parsed event
dicts (the heavy `text` / `output` / `tool_calls` payloads), capped at
`_DEFAULT_BODY_CACHE_CAPACITY` (default 2000, injectable for tests). The cap is
far larger than any single tail/backfill page (default 50), so normal scrollback
stays resident, while memory stays bounded for an arbitrarily long transcript.
On a miss, the event's source line is re-read from disk via the locator's byte
range (`_reparse_line_locked`: `seek` + `read` exactly `byte_len`, parse with
deduplication disabled so an already-seen id is still reconstructed, reusing the
persistent tool-name map) and re-inserted.

### 2.3 Bounded read paths

- `get_tail_events(limit)` walks only the tail of the locator index and resolves
  at most `limit` bodies -- O(limit), never reading the whole transcript. Used
  for the initial load.
- `get_backfill_events(before_event_id, limit)` locates the cursor via
  `_locator_ref_by_id` (O(1)), walks back at most `limit` locators across the
  selected files, and resolves their bodies (re-reading from disk on a miss), so
  a page costs O(limit) regardless of how far back it reaches.
- `has_events_before(event_id)` answers whether older history exists without
  resolving any bodies, to populate the response's `has_more` flag.
- `get_all_events()` still resolves every body and is retained for the *bounded*
  subagent transcripts and as a brute-force oracle for tests; it is not on the
  main-view hot path.

### 2.4 Incremental tailing and emission

`_ensure_cache_current` brings a file's locator index up to its current contents
by reading only the bytes appended past `byte_offset_consumed`, splitting them
into line spans (`_iter_line_spans`), parsing each, appending a locator per
event and writing each body to the LRU. It handles partial trailing lines (left
for the next poll) and truncation / atomic-rewrite (file shrank below the
consumed offset): the file's event ids are purged from the agent-wide dedup set
*and* from `_locator_ref_by_id`, the offset and locator list reset, and the
emission marker resets so the re-read content is re-broadcast.

Emission to SSE clients is decoupled from parsing: each `SessionFileState`
tracks an `emitted_count` high-water mark over its locator index, and the poll
loop broadcasts every locator past that marker (resolving bodies first). This
guarantees exactly-once delivery even when a concurrent HTTP read was the thread
that advanced the byte offset.

### 2.5 HTTP contract

`GET /api/agents/{id}/events` returns the tail (last `limit`, default 50) plus
`has_more`; `?before=<event_id>&limit=<n>` returns one bounded backfill page.
The response shape is `{events, has_more, step_enrichment}` -- `has_more` lets
the client decide whether to page back without a probe request, and is additive
(an older client that ignores it still works). Subagent endpoints mirror this.

## 3. Frontend: bounded memory, on-demand backfill, virtualization

### 3.1 Event store (`Response.ts`)

`eventsByAgent` holds the resident events per agent, mirrored by a persistent
`eventByIdByAgent: Map<event_id, event>`. The map serves two purposes at O(1)
per event: dedup on append/prepend (no rebuilding a set on every SSE delivery),
and lookup of an already-stored event so a re-broadcast (same `event_id`, e.g. a
subagent tool-call whose linkage arrived late) can be upgraded in place rather
than dropped as a duplicate.

`fetchEvents` loads the tail and records the server's `has_more`.
`fetchBackfillEvents` fetches exactly one older page (before the first held
event), prepends it, and trusts the server's `has_more` to decide whether more
remains. `evictOldEvents` trims the oldest events once the resident count
exceeds `MAX_HELD_EVENTS` (1500) down to `EVICT_TARGET_EVENTS` (1000), and
forces `has_more` true so the dropped history is re-fetched on scroll-up. The
callers only evict while following the live tail, so a scrolled-up reader's
rendered history is never removed from under them.

### 3.2 Windowing math (`virtualWindow.ts`)

`computeVisibleWindow` is a pure, DOM-free function: given the row count, a
`getHeight(index)` accessor, the scroll position, the viewport height and an
overscan margin, it returns the contiguous slice of rows intersecting
`[scrollTop - overscan, scrollTop + viewportHeight + overscan]` plus the
`topPad` / `bottomPad` spacer heights standing in for the rows above and below.
Keeping the non-trivial part of virtualization free of the DOM makes it
unit-testable; the component only feeds it measured heights and renders the
result.

### 3.3 Rendering integration (`ChatPanel.ts`, `SubagentView.ts`)

The message list is virtualized: only the rows whose estimated extent intersects
the viewport (plus `OVERSCAN_PX`) are mounted to the DOM, with top/bottom spacer
divs sized by the summed heights of the off-window rows. Row heights are measured
from the DOM after mount (keyed by each row's DOM `id`) and cached; unmeasured
rows fall back to per-type estimates, which only affect spacer sizing for
off-screen rows and self-correct as rows scroll into view. Because off-window
rows are never mounted, `MarkdownContent` is only parsed for on-screen rows.

In the main panel, the rows are the top-level nodes of the turn-grouped view:
a user message, a whole `ProgressBlock` for a turn that has tk steps, an
ungrouped assistant message, a stop-hook chip, or a trailing wrap-up reply.
Virtualizing at this level preserves the turn structure, the progress timeline,
skill expansions and auth-error hiding while still mounting only the visible
rows. Every row's rendered root carries a DOM `id` equal to its key so it can be
measured (user and assistant rows already do; `ProgressBlock` accepts an optional
`id`). The subagent view virtualizes a flat user/assistant list the same way
(its transcript is bounded but can still be large).

Scrolling near the top (within `BACKFILL_TRIGGER_PX`) while the server reports
`has_more` triggers exactly one backfill page -- not a drain-to-completion loop.
On prepend, `scrollTop` is compensated by the height the content grew so the
viewport stays anchored on what the reader was looking at. When the reader is
following the live tail, new events scroll to the bottom and the store evicts
old events past the cap.

## 4. Acceptance criteria (encoded as tests)

Backend (`session_watcher_test.py`):

- Tail and backfill match a brute-force `get_all_events` oracle over a synthetic
  large transcript spanning multiple (resumed) session files and multi-event
  lines.
- Backfill of *evicted* history reconstructs event bodies correctly from disk.
- Backfill disk-read bytes are bounded and independent of `N` (a test that would
  fail on an O(N) re-scan).
- The body-tier cap is respected: resident body count stays bounded while paging
  across the entire history.

Frontend (`virtualWindow.test.ts`, `Response.test.ts`):

- `computeVisibleWindow` visible-range and spacer-sizing math, including overscan
  and scrolled-past-the-end behaviour.
- Dedup is O(1) and a late re-broadcast upgrades an existing event in place.
- `has_more` drives backfill paging: a page is fetched before the first held
  event, and paging stops (with no further network calls) once the server
  reports no more history.
- Eviction trims the oldest to the target, flags more history, and re-admits an
  evicted id on a later re-fetch.

The interactive scroll/measure behaviour (windowing, scroll-position
preservation on prepend, eviction while tailing) is verified manually by driving
the running app with a long transcript; it is timing-dependent and not
crystallized into automated tests.
