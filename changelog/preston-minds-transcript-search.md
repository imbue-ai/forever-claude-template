- Added a `find-past-transcripts` skill so an agent can recall the chat history of
  earlier agents that ran on this workspace host -- sub-agents launched via
  `launch-task`, sibling agents, or earlier sessions. It searches both roots:
  still-present agents (running or STOPPED) under `/mngr/agents/`, and destroyed
  agents under `/mngr/preserved/`, then reads the transcript (via `mngr transcript`
  for present agents, or `cat`/`jq` on the JSONL). Note a finished `launch-task`
  worker is usually left STOPPED (in `/mngr/agents/`), not destroyed, so the skill
  checks there too rather than only `/mngr/preserved/`.

- The skill's description and a "Finding past work" note in `CLAUDE.md` tell the
  agent, by default, that past/deleted chats on this host are recoverable -- and
  to never claim it can't access an earlier or deleted conversation without first
  checking -- so it reaches for the skill instead of refusing.
