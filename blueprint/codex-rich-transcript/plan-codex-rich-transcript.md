# Plan: rich codex transcript in the Minds chat UI

## Goal

Make a **codex** agent's conversation render in the Minds chat screen with the
same fidelity as a claude agent, by parsing codex's **raw rollout** and emitting
the *same* UI event schema `session_parser.py` produces for claude. Auth is
explicitly out of scope here (separate slice — see "Deferred").

## Approach: Option A (parse the raw rollout), not Option B (ride the common transcript)

The first codex shuttle (already committed on `add-codex`) consumed mngr_codex's
**common transcript** (`events/codex/common_transcript/events.jsonl`). That is
lossy by design — the agent-side converter (`common_transcript_convert.py`)
drops:

- **token usage** (`event_msg` `token_count`) → no `usage` field
- **apply_patch tool calls** (`custom_tool_call` / `custom_tool_call_output`) → file-edit turns render as bare text, no tool card
- **turn/abort signals** (`event_msg` `turn_aborted`, `task_complete`)
- exact interleaving / reasoning

Claude's own UI path does **not** consume mngr_claude's common transcript either —
`session_parser.py` reparses claude's *raw* format to recover richness the common
schema flattens. We mirror that for codex: parse the raw rollout, emit the rich
schema. This makes codex events first-class-identical to claude events, so the
transport (SSE), the frontend, and `activity_state.py` need **zero** codex-specific
branches.

Trade-off accepted: more code than Option B, and two fields genuinely can't come
from the rollout — `is_auth_error` (lives in `logs_2.sqlite`, deferred) and rich
subagent cards (codex's subagent model differs; see Phase 2). Everything else
reaches parity.

---

## The shared output schema (the contract every parser targets)

Three event types. Shared envelope: `timestamp`, `type`, `event_id`, `source`,
`message_uuid`, `[session_id]`.

```jsonc
user_message:      { …env…, role:"user", content }

assistant_message: { …env…, role:"assistant", model, text,
                     tool_calls: [ { tool_call_id, tool_name, input_preview,
                                     [description], [subagent_type], [subagent_metadata] } ],
                     stop_reason,
                     usage: { input_tokens, output_tokens, cache_read_tokens, cache_write_tokens } | null,
                     is_auth_error: bool }

tool_result:       { …env…, tool_call_id, tool_name, output, is_error, [subagent_id] }
```

This is the contract in `frontend/src/models/Response.ts` (harness-tolerant:
`usage`/`stop_reason` nullable, `subagent_*`/`description` optional,
`is_auth_error` a required bool). Any harness parser must emit exactly this shape.

---

## The tk marker contract (3-site — keep in sync)

tk step-progress rendering is a cross-cutting contract spanning three sites:

```
vendor/tk/ticket            →  <harness>_session_parser.py     →  frontend/turn-grouping.ts
  EMITS the markers            PRESERVES them past truncation      RE-PARSES → step/progress tree
  (cmd_create/start/close)     (tk_markers.py, shared)             (TK_UPDATED_RE, TK_STEP_*_RE)
```

Marker format:
- `Updated <id> -> (open|in_progress|closed)`  (every tk lifecycle command's output)
- `tk-step <id> title: <title>` / `tk-step <id> summary: <summary>`

The parser's *only* tk job is **preservation through truncation** (keep the markers
in `input_preview` and tool `output` so they survive the 200/2000-char caps).
`turn-grouping.ts` is **harness-blind** — it parses markers off events regardless of
harness — so codex tk progress renders for free once the codex parser preserves the
same markers.

---

## Prep phase (generalization — do first, one commit, mechanical)

1. **Rename claude's files** to carry the harness prefix (symmetry with `codex_*`):
   - `session_parser.py` → `claude_session_parser.py`
   - `session_watcher.py` → `claude_session_watcher.py`
   - `session_parser_test.py` → `claude_session_parser_test.py`
   - `session_watcher_test.py` → `claude_session_watcher_test.py`
   - Class renames: `AgentSessionWatcher` → `ClaudeSessionWatcher`,
     `parse_session_lines` → `parse_claude_session_lines`
   - Import updates: `app_context.py`, `welcome_resend.py`, the two tests, and the
     watcher's own import of the parser. (~5 lines, ~10 files.)

2. **Extract `tk_markers.py`** — pull the harness-agnostic tk helpers out of the
   (renamed) claude parser into a shared module both parsers import:
   `_TK_LIFECYCLE_VERBS`, `_TK_OUTPUT_DECORATION_PATTERN`, `_is_tk_lifecycle_call`,
   `_truncate_tool_output`, the length constants. Repoint claude's parser to it.
   **Plain module, not an ABC** — this logic is identical across harnesses (operates
   on command strings + output text), so there's no polymorphism to abstract. (The
   ABC/Protocol, if ever, belongs on the *parser* interface, where the raw formats
   genuinely differ — deferred until harness #3.)

3. **Seams — already done** (committed): harness `type` threaded from
   `AgentDetails.type` → `AgentInfo.type` (`agent_discovery.py`), and the
   watcher-selection branch (`_is_codex_agent` in `app_context.py`, type-first with a
   `plugin/codex/home` filesystem-probe fallback). Option A reuses these unchanged —
   only the watcher internals change.

---

## Phase 1 — Core (user_message / assistant_message / tool_result)

`usage` (token counts) is **deferred to the special cases** (Phase 2) — the raw
`token_count` values are coarse/cumulative in practice (`total_tokens` set,
`input/output` often 0), so it's not worth blocking core messages on. Phase 1
emits `usage: null`.

Rewrite `codex_session_parser.py` from the thin common-transcript adapter into a
real raw-rollout parser, and repoint `codex_session_watcher.py`.

### Watcher
- Tail `<agent_state_dir>/logs/codex_transcript/events.jsonl` — the **raw** rollout,
  which mngr_codex's `stream_transcript.sh` already tails to this stable per-agent
  path (verbatim, no reschematizing). No `sessions/` directory hunt.
- Keep the existing `CodexSessionWatcher` design: incremental byte-offset read,
  partial-line carry, dedup by `event_id`, in-memory list. Do **not** adopt claude's
  two-tier cache — the rollout is fine to hold in memory, and (crucially) we never
  reparse a single line in isolation, which is what would break ordinal event_ids.

### Parser mapping (codex raw `{timestamp, type, payload}` → schema)

**Sourcing rule (confirmed against codex `policy.rs` + a real rollout):**
`response_item` is the canonical conversation state; `event_msg` is the derived
live-display stream. Build the body from `response_item` — **except user bubbles**
(see below). Process lines in **file order** (that's the true conversation order).

- **user bubbles → `event_msg` `user_message`** (NOT `response_item` role=user).
  `response_item` role=user is the *model-facing* user role: the human prompt **plus**
  injected `AGENTS.md`, `<environment_context>`, `<turn_aborted>`, and
  `<subagent_notification>` content. `event_msg user_message` is exactly the clean
  human-typed prompts (real rollout: 4 clean vs 10 with-injection). This is where we
  beat mngr's own converter, which shows the AGENTS.md block as a user bubble.
  → **skip `response_item` role=user entirely.**
- `response_item` `message` role assistant (`output_text`) → **assistant_message** (`text`)
- `response_item` `function_call` **and** `custom_tool_call` (apply_patch) → `tool_calls`
  on an assistant_message  ← **recovers apply_patch cards**
  - Other tool-call variants exist and reach disk too: `local_shell_call`,
    `web_search_call`, `image_generation_call`, `tool_search_call` (+ MCP tools, which
    appear as `function_call`). v1: handle `function_call` + `custom_tool_call`; render
    the rest generically or defer — but don't crash/blank on them.
- `response_item` `function_call_output` / `custom_tool_call_output` → **tool_result**
  (pair to its call by `call_id`)
- `event_msg` `agent_message` → **skip** (display projection of the assistant `response_item`;
  it's a superset with `phase`/`memory_citation` display metadata — canonical text is the response_item)
- `event_msg` `token_count` → **deferred to Phase 2** (emit `usage: null` in core)
- `response_item` `reasoning` → skip (v1; `encrypted_content` is opaque — render as
  a "thinking" placeholder later if wanted)
- `session_meta`, `turn_context`, `task_started`/`task_complete` → drop (task == turn;
  `task_complete.turn_id`/`duration_ms` available for Phase 2 turn-boundaries/stop_reason)
- `stop_reason` → synthesize from turn boundaries (`task_complete`) or leave null
- `session_meta`, `turn_context`, `reasoning` → drop
- `is_auth_error`: `false` (deferred); `subagent_*` fields: omitted (Phase 2)
- **tk**: import `tk_markers.py`; pull the command from `function_call.arguments`
  (vs claude's `Bash.input.command`), same preservation logic

### Key design points
- **event_id stability**: rollout lines lack claude's per-line `uuid`. Use a
  **monotonic line index** (safe because the watcher reads incrementally in-memory
  and never reparses a single line). Do not use the common converter's line-ordinal
  approach in a single-line-reparse context.
- **Turn assembly** (the main complexity): codex splits an assistant turn across
  multiple `response_item`s (text, then separate `function_call`s) and puts `usage`
  in a *separate* `event_msg` `token_count`. The parser must stitch `token_count`
  onto the right assistant message. (Claude's `usage` rides the assistant record
  directly; codex's does not.)

---

## Phase 2 — The special cases

Independent add-ons after core messages render. `usage` joins this bucket
(deferred from core).

### 0. Token usage  ⚠️ coarse — low priority
`event_msg` `token_count` → `usage`. Real values are cumulative/coarse
(`total_tokens` populated, `input/output` often 0), so map best-effort
(`cached_input_tokens` → `cache_read_tokens`, no cache-write equivalent) or skip
until it's worth the effort. Not blocking anything.

## Phase 2 (cont.) — The four interaction cases

### 1. Subagent linkage  ⚠️ codex model differs — heaviest item
Codex subagents are **separate rollout files** linked by `parent_thread_id`
(`session_meta.source.subagent`), and surface in the parent as opaque
`<subagent_notification>{…"agent_path":…}</subagent_notification>` user_messages —
**not** claude's `Agent`-tool / `toolUseId` / `agentId` model. So this is not a
port. Options: (a) parse the `<subagent_notification>` payloads out of the parent to
synthesize card linkage, or (b) discover + link the child rollout files.
**v1: leave `subagent_*` empty** (subagent turns just don't get a card yet).
Reference: `blueprint/robust-subagent-linkage/`.

### 2. tk decoration  ✅ free once `tk_markers.py` exists
Reuse the shared module; `turn-grouping.ts` renders it harness-blind. Only change:
pull the command from `function_call.arguments`. **No frontend/downstream changes.**

### 3. Queued messages  ⚠️ codex has no enqueue log
`mngr_codex/plugin.py:400-401`: *"No queue-log fallback (claude's misfire
workaround): codex's raw transcript is the rollout JSONL, not the enqueue-event
log."* Claude writes a `queued_command` **at enqueue time**; codex has no such
record — a queued message appears in the rollout only as a normal `user_message`
**when codex processes it**. So the Minds "Queued" bubble reconciles **late**
(at processing) rather than at enqueue.
**OPEN — needs empirical run** (see below): does codex's TUI even accept/queue
mid-turn input, and is there truly no enqueue-time trace?

### 4. Request-interrupt-by-user  ✅ confirmed: `event_msg turn_aborted`
Claude writes a `[Request interrupted by user]` sentinel user-message that
`session_parser` drops (so the activity indicator doesn't stick on "Thinking…").
Codex (confirmed via `policy.rs` + real rollout) emits a persisted `event_msg`
`turn_aborted` (`turn_id`, `reason:"interrupted"`, `duration_ms`) — dropped by the
common transcript, visible under Option A. There's *also* a synthetic
`<turn_aborted>` injected as a `response_item` user role (which we already skip via
the user-sourcing rule). Plan: consume `turn_aborted` to resolve the activity
indicator to idle; don't emit a visible bubble.

### 5. apply_patch diffs (enhancement, optional)  ✨
The `custom_tool_call` (raw patch text) + its output give a basic tool card. For a
*pretty* per-file diff, `event_msg` `patch_apply_end` carries structured
`changes` (per-file `unified_diff`, `stdout`, `success`) — overlay it on the
apply_patch tool_result when rendering. Nice-to-have, not required.

---

## Deferred: auth (`is_auth_error`)

Codex keeps auth errors **out of the transcript entirely** — they land only in
`<agent_state_dir>/plugin/codex/home/logs_2.sqlite` (the `logs` table), not the
rollout or stderr (interactive codex writes nothing to stderr). Detection is a
separate agent-side tailer:

```sql
SELECT 1 FROM logs
WHERE level='ERROR' AND target='codex_api::endpoint::responses_websocket'
  AND (feedback_log_body LIKE '%auth_error="401"%'
    OR feedback_log_body LIKE '%401 Unauthorized%/v1/responses%')
  AND id > :last_seen_id;
```

Exclude `codex_core_plugins::*` 401 noise (fires even on healthy api-key login —
ChatGPT-only feature). This is its own slice, plus the login modal (device-auth +
API-key) — see the auth investigation notes. Not part of this plan.

---

## Coverage — the full chat surface

| Interaction | Source | Status |
|---|---|---|
| user / assistant / tool_result | core parse | Phase 1 |
| apply_patch tool cards | `custom_tool_call` | Phase 1 (recovered) |
| token usage | `event_msg token_count` | Phase 2 (coarse, low priority) |
| activity indicator (Thinking…/idle) | derived from core types | free |
| optimistic/pending bubbles | reconcile vs core `user_message` | free |
| file attachments | folded into message text | free |
| subagent cards | linkage | Phase 2 (v1: empty) |
| tk step-progress | `tk_markers.py` + turn-grouping | Phase 2 (free frontend) |
| queued messages | processing-time `user_message` | Phase 2 (OPEN) |
| request interrupt | `turn_aborted` | Phase 2 (verify) |
| **auth error → login modal** | `logs_2.sqlite` | **Deferred (separate slice)** |

This is the complete chat-transcript surface. Nothing else renders on the chat
screen from the transcript.

---

## Open empirical questions (verify before/while building)

1. **Queued messages** — run interactive codex, send a message mid-turn, inspect the
   rollout: does codex accept/queue it, and does it appear only at processing time?
2. **Interrupt** — confirm `turn_aborted` is codex's user-interrupt signal and its
   shape.
3. **Subagent** — decide (a) parse `<subagent_notification>` vs (b) link child
   rollout files.
4. **Schema/version** — re-confirm rollout event shapes and the `logs_2.sqlite`
   schema on codex **0.144.3** (box pin); investigation was on 0.142.5. These are
   undocumented internal formats and can drift across versions.

## Sequencing

1. Prep commit (renames + `tk_markers.py`) → push.
2. Phase 1 (core raw parser + watcher repoint) → push, test in box.
3. Phase 2 items independently (tk is nearly free; queued/interrupt after empirical
   runs; subagent last).
4. Auth as its own later slice.
