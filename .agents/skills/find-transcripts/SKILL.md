---
name: find-transcripts
description: "Find, read, or search through any chat message, transcript, or conversation content from this host -- whether from an active agent, a past session, a deleted agent, a sub-agent, or a worker. Use this skill any time a user asks about chat histories or you otherwise want to access them. NOTE: this skill only covers agents on THIS host -- not other services (ChatGPT, claude.ai, etc.) or other Minds workspaces."
compatibility: Covers agents that ran on this host (active, stopped, or destroyed). Uses find/cat/jq/mngr.
---

# Find transcripts

**Do not tell the user you can't see a conversation before you check.** Every
agent that has run on this host leaves its transcript behind locally -- past,
active, or destroyed. Refusing without looking is wrong.

An agent's conversation is stored under its state dir as
`events/<source>/common_transcript/events.jsonl` (source is the agent type, e.g.
`claude`). On **this** host that state dir is in one of two places, depending on
whether the agent still exists:

- **Still present** (running, or **STOPPED** but not destroyed):
  `/mngr/agents/<agent_id>/events/*/common_transcript/events.jsonl`.
  A finished `launch-task` worker is usually left STOPPED here -- it is **not**
  in `/mngr/preserved/` until it is actually destroyed.
- **Destroyed:**
  `/mngr/preserved/<agent_name>--<agent_id>/events/*/common_transcript/events.jsonl`.

(Use `$MNGR_HOST_DIR` in place of `/mngr` if this host's mngr root is elsewhere.)
**Always check both** -- a past agent could be in either.

**Note on agent types:** transcripts here include the user-facing chat agent
*and* any worker/sub-agents launched via `launch-task` or similar. Workers often
have short, task-focused transcripts. `mngr list` shows agent names and labels to
help you identify which is which.

## What this skill does NOT cover

- **Other services** (ChatGPT, claude.ai, other AI tools): their chats are not
  stored on this host. To access them you'd need to pull in that data separately
  via their own export features.

- **Other Minds workspaces**: each workspace is a separate host with its own
  `/mngr/`. Transcripts from agents in another workspace live there, not here.
  To read them, SSH into that workspace via the Minds API: use the `minds-api`
  skill to request the `minds-workspaces-ssh` latchkey permission, then run this
  skill's read commands over SSH on that host.

## 1. See what's on this host

```bash
mngr list                        # agents still present (running / stopped), with names + ids
ls -1t /mngr/preserved 2>/dev/null   # destroyed agents (<agent_name>--<agent_id>), newest first
```

Match the user's description to an agent by its name (and, for preserved dirs,
the mtime -- roughly when it was destroyed: `ls -lt /mngr/preserved`).

## 2. Find every transcript on this host (present OR destroyed)

```bash
find /mngr/agents /mngr/preserved -path '*/common_transcript/events.jsonl' 2>/dev/null
```

## 3. Read one

For a still-present agent, the easiest is the rendered view:

```bash
mngr transcript <agent-name-or-id>          # works for running/stopped agents; NOT for destroyed ones
```

For any agent (present or destroyed), read the file directly (pick a path from
step 2):

```bash
cat "/mngr/agents/<agent_id>/events/claude/common_transcript/events.jsonl"
# or, if destroyed:
cat "/mngr/preserved/<agent_name>--<agent_id>/events/claude/common_transcript/events.jsonl"
```

## 4. Render a raw file readably

```bash
F="<path from step 2>"
jq -r '
  if .type=="user_message" then "USER: \(.content)"
  elif .type=="assistant_message" then "ASSISTANT: \([.parts[]?|select(.type=="text").content]|join(" "))"
  elif .type=="tool_result" then "TOOL(\(.tool_name)): \(.output[0:300])"
  else .type end' "$F"
```

## Notes

- `system-services--*` and infra agents may have no common transcript -- look at
  the named agents.
- A transcript only exists if that agent actually produced one; a brand-new agent
  with no turns won't have one.
- `mngr transcript` does NOT work for destroyed agents (they're no longer in
  mngr's live index); read the file directly instead.
