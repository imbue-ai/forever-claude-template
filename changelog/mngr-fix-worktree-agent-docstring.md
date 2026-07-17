- Fixed two misleading comments in `agent_manager.py`. The comment above the
  worktree agent's `mngr create` argv called it a "worker" -- a copy-paste from
  the chat agent's parallel comment. Worktree agents (user-created from the "New
  agent" tab, labeled `user_created=true`) and worker agents (created by another
  agent via the `launch-task` skill, labeled `agent_created=true`) are distinct,
  and the label distinction drives the OOM shedding bands, so conflating them in
  the comment right above the label argument was actively confusing.

- Both comments also said the agent "carries no workspace label". The `workspace`
  label was removed entirely when workspace names were split into a
  `workspace_display_name` label plus a normalized host slug, so the clause
  referred to a concept that no longer exists. Each comment now states what is
  actually shared with the workspace instead.
