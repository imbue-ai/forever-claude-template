# Robust subagent linkage in minds_workspace_server

> Make subagent rendering in minds_workspace_server work for all Claude Code subagent types and versions, by sourcing the `tool_use_id → subagent_id` link from multiple places: the structured `toolUseResult.agentId` field, the legacy `agentId:` text trailer, and (for running subagents) the parent linkage written into each subagent jsonl's first line on disk.

## Overview

- Originally, minds linked a parent Agent tool_use to its subagent session by regex-scraping the `agentId: <id>` text trailer Claude Code optionally appends to Agent tool_result content. When the trailer is absent, the frontend falls back to the boring inline render and loses the nice subagent card.
- Claude Code omits the trailer for one-shot subagent types (e.g. `Explore`) in some versions, so even fully successful Agent calls render badly. The bug was visible in main agents that use `Explore` subagents while not affecting worker agents that use `general-purpose`.
- Empirical scan of all on-disk Claude session files shows neither tool_result source is complete: the trailer covers ~93% of Agent tool_results, the structured field `toolUseResult.agentId` covers ~64%, and together they cover 100%. The structured field is universally absent from nested-subagent jsonls; the trailer is absent from older versions and from some recent non-resumable agent types. Additionally, both sources only land *after* the subagent finishes — running subagents have no tool_result yet.
- Fix has two parts: (1) in `session_parser.py`, read the subagent id from both the structured field and the trailer (preferring the structured field). (2) In `session_watcher.py`, also read each subagent jsonl's first line on disk — it carries `sourceToolAssistantUUID` plus `agentId`, which lets the watcher link a running subagent to its parent Agent tool_use before any tool_result lands.
- This is purely a Claude Code adapter change — `session_parser.py` and `session_watcher.py` already encode Claude-Code-specific schema throughout, so there is no abstraction being broken.

## Expected behavior

- Subagents of every Claude Code agent type (`Explore`, `general-purpose`, plugin-defined types like `imbue-code-guardian:verify-and-fix`, etc.) render as the rich subagent card in the minds frontend, not as a plain inline tool-call block.
- The card shows the same fields as today (agent type, description); no visual changes.
- Sessions captured by older Claude Code versions that emitted only the trailer continue to render correctly.
- Sessions captured by newer Claude Code versions that emit only the structured field also render correctly — this is the case that fails today.
- Previously-recorded sessions on disk get the improved rendering automatically the next time the user opens the mind, because minds re-parses session files on each `get_all_events` call.
- When the structured field and the trailer disagree (not expected to occur, but defensively): the structured field wins silently.
- No change to behavior for any tool other than `Agent`.

## Changes

- Update the Agent tool_result parsing path in `session_parser.py` so that `subagent_id` is extracted from `toolUseResult.agentId` on the raw event when present, with the existing `agentId:` text-trailer regex retained as a fallback when the structured field is missing. Preserve current event shape: `subagent_id` is still attached to the same `tool_result` event field that `_enrich_subagent_metadata` already consumes.
- Update `session_watcher.py` to read each subagent jsonl's first line (via `_read_subagent_parent_info`) and cache the `sourceToolAssistantUUID → agentId` linkage. `_enrich_subagent_metadata` now uses this disk-based linkage as the primary source (matching subagents to their parent assistant message's Agent tool_uses by spawn order) and falls back to the tool_result-based linkage when the subagent jsonl is absent.
- Tidy up `_discover_subagent_sessions` so meta.json reading retries on transient `OSError` but gives up on `JSONDecodeError` (tracked in `_subagent_meta_read_failed`), avoiding repeated warnings each poll cycle.
- No changes to the frontend.
- Parser-level unit tests cover four fixture cases: structured-field-only, trailer-only, neither, and both-disagree (structured field wins).
- Watcher-level unit tests cover three cases: a running subagent picks up its rich card from the disk linkage, multiple Agent tool_uses in one assistant message pair with their subagents in spawn order, and the tool_result-based linkage still resolves metadata when the subagent file is gone.
- Manual verification by spawning an `Explore` subagent in a real minds session and confirming the rich subagent card renders.
