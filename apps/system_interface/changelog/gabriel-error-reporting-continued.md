- When a newly-discovered agent carries the `assist=true` label (a chat spawned
  by the minds "get help -> have an agent help" flow), its tab is now auto-opened
  so the user lands on it. Gated on first discovery, so a tab the user later
  closed is not forced back open, and a restart restores the saved layout instead
  of re-opening the assist tab.
