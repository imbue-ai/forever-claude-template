Auto-revive dead chat agents, with a visible restart notice in the chat.

A chat agent's claude process could die (out-of-memory shed, crash, container
restart) and just stay down: the UI kept rendering the last transcript message
-- often a stale API error -- with no indication anything happened, until the
user manually messaged the chat. The new `ChatReviver` (fed the same
agent-state view as the rest of `AgentManager`, from initial discovery and
every observe event) now detects dead chats and revives them by sending a
restart notice through the manager's normal message path: the delivery itself
relaunches the agent (mngr revives DONE husks and starts STOPPED agents on
send), and the agent's reply surfaces the restart in the chat.

Policy: any managed chat found `DONE` (process died under a live tmux session
-- unambiguously a crash/kill) is revived; `STOPPED` (no tmux session: a
reboot or an explicit stop) revives only the initial chat agent, so the mind
itself always comes back after a warm boot while a deliberately-stopped
secondary chat stays down. Revival defers while available memory is below a
floor just above earlyoom's SIGTERM threshold (reviving into pressure would
just get the chat shed again), backs off exponentially on repeated deaths,
and resets the backoff after a stability window. `welcome_resend`'s
initial-chat-id resolver is now public (`resolve_initial_chat_agent_id`) and
shared by the reviver.
