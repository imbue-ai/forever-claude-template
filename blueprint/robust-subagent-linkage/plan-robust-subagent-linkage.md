# Robust subagent linkage in minds_workspace_server

> Make subagent rendering in minds_workspace_server work for all Claude Code subagent types and versions, by sourcing the `tool_use_id → subagent_id` link from multiple places: the structured `toolUseResult.agentId` field, the legacy `agentId:` text trailer, and (for running subagents, before any tool_result lands) the `toolUseId` written into each subagent's `<id>.meta.json` on disk.

## Overview

- Originally, minds linked a parent Agent tool_use to its subagent session by regex-scraping the `agentId: <id>` text trailer Claude Code optionally appends to Agent tool_result content. When the trailer is absent, the frontend falls back to the boring inline render and loses the nice subagent card.
- Claude Code omits the trailer for one-shot subagent types (e.g. `Explore`) in some versions, so even fully successful Agent calls render badly. The bug was visible in main agents that use `Explore` subagents while not affecting worker agents that use `general-purpose`.
- Empirical scan of all on-disk Claude session files shows neither tool_result source is complete: the trailer covers ~93% of Agent tool_results, the structured field `toolUseResult.agentId` covers ~64%, and together they cover 100%. The structured field is universally absent from nested-subagent jsonls; the trailer is absent from older versions and from some recent non-resumable agent types. Additionally, both sources only land *after* the subagent finishes — running subagents have no tool_result yet.
- Fix has three parts: (1) in `session_parser.py`, read the subagent id from both the structured field and the trailer (preferring the structured field). (2) In `session_watcher.py`, also read each subagent's `<id>.meta.json` on disk — its `toolUseId` names the parent Agent tool_use directly (written at spawn time), which lets the watcher link a running subagent to its parent before any tool_result lands. The subagent jsonl's first line is NOT a usable source here: `parentUuid` is null and `sourceToolAssistantUUID` is absent, so the meta.json is the only pre-completion link. (3) Persist the tool_result-based linkage and the cached unlinked parent events across poll cycles, and re-broadcast a parent once its linkage lands, so a running subagent's card upgrades live without a page refresh.
- This is purely a Claude Code adapter change — `session_parser.py` and `session_watcher.py` already encode Claude-Code-specific schema throughout, so there is no abstraction being broken.

## Expected behavior

- Subagents of every Claude Code agent type (`Explore`, `general-purpose`, plugin-defined types like `imbue-code-guardian:verify-and-fix`, etc.) render as the rich subagent card in the minds frontend, not as a plain inline tool-call block.
- The card renders immediately from the Agent tool call's own `description`/`subagent_type`, so it appears the instant the call is streamed -- even before the subagent session is linked. While the subagent is running and unlinked it shows a non-clickable "Running…" placeholder; once linkage lands it upgrades in place to a clickable "View conversation" link (no page refresh needed).
- Sessions captured by older Claude Code versions that emitted only the trailer continue to render correctly.
- Sessions captured by newer Claude Code versions that emit only the structured field also render correctly — this is the case that fails today.
- Previously-recorded sessions on disk get the improved rendering automatically the next time the user opens the mind, because minds re-parses session files on each `get_all_events` call.
- When the structured field and the trailer disagree (not expected to occur, but defensively): the structured field wins silently.
- No change to behavior for any tool other than `Agent`.

## Changes

- Update the Agent tool_result parsing path in `session_parser.py` so that `subagent_id` is extracted from `toolUseResult.agentId` on the raw event when present, with the existing `agentId:` text-trailer regex retained as a fallback when the structured field is missing. Preserve current event shape: `subagent_id` is still attached to the same `tool_result` event field that `_enrich_subagent_metadata` already consumes.
- Surface the Agent tool call's `description` and `subagent_type` in `session_parser.py` so the frontend can render the rich card before any linkage exists.
- Update `session_watcher.py` to read each subagent's `<id>.meta.json` and cache its `toolUseId → sub_id` linkage. `_enrich_subagent_metadata` uses this disk-based linkage as the primary source (matched by exact `toolUseId`, so order is irrelevant) and falls back to the tool_result-based linkage when the meta.json omits `toolUseId` or the subagent files are gone. The tool_result-based linkage is accumulated persistently across poll cycles, and unlinked parent events are cached and re-broadcast (`_rebroadcast_relinked_parents`) once their linkage lands so the card upgrades live.
- Tidy up subagent meta.json reading so it retries on transient `OSError` but gives up on `JSONDecodeError` (tracked in `_subagent_meta_read_failed`), avoiding repeated warnings each poll cycle.
- Frontend: render the rich subagent card from the tool call's `description`/`subagent_type` (non-clickable "Running…" state until linked, then a clickable "View conversation" link); merge late-arriving `subagent_metadata` from a re-broadcast parent onto the already-stored event (`mergeLateSubagentMetadata` in `Response.ts`) and repaint when a card links (`countSubagentCards`); add the `--pending` link styling.
- Refactor the SSE streaming loop in `server.py` into a shared `_stream_filtered_events` helper used by both the main and per-subagent streams, with the main stream dropping subagent-session events via `is_main_session_event`.
- Parser-level unit tests cover the description/subagent_type exposure plus four tool_result fixture cases: structured-field-only, trailer-only, neither, and both-disagree (structured field wins).
- Watcher-level unit tests cover disk-based linkage for a running subagent, multiple Agent tool_uses linked by exact `toolUseId` regardless of order, tool_result fallback when the subagent file is gone, live re-broadcast when the subagent jsonl or its tool_result arrives in a later poll cycle, and the main-stream session filter.
- Manual verification by spawning an `Explore` subagent in a real minds session and confirming the rich subagent card renders and upgrades from running to linked.
