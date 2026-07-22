- Workspaces now ship a **`VERSION_HISTORY.md`** at the repo root -- a plain,
  human-readable record of where the workspace came from and what it has
  published. A `## Workspace` section holds the template version it was created
  from plus one line per update; an `## Inspirations` section holds one entry per
  published inspiration (`v1`, `v2`, ... under a per-slug heading). Each line
  ends in the commit it was cut from, and earlier lines are never rewritten, so
  the whole lineage is walkable in git. The `update-version` skill owns the
  format and both flows that write to it.

- Added design docs under `blueprint/agent-inspiration-update-awareness/` for
  knowing an inspiration's status and updating a published one: the full
  proposal, plus a short summary of the version-history file and of why an
  update re-runs the inspiration's recipe against the current workspace rather
  than diffing two repositories (which is what preserves deliberate exclusions).
