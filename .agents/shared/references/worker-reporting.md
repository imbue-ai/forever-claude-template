# Worker reporting contract

Generic file-based protocol for signaling the lead at each gate and at
terminal status. The worker supplies flow-specific runtime paths and the
enum of allowed `name:` values.

## Task-file inputs

Your task file has been synced to your worktree at `<RUNTIME_DIR>/task.md`.
Your worker SKILL.md lists any additional inputs the calling flow stages
alongside it. At the start of your run, extract the lead's address with:

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py '<TASK_FILE_GLOB>')"
```

Quote the pattern. `LEAD_AGENT` is the `mngr` agent you push reports to
(and whose transcript you read); `LEAD_REPORT_DIR` is the destination
directory on the lead's worktree. Any additional string fields the lead
set in the frontmatter also become shell variables -- see your worker
SKILL.md for which extras (if any) the calling flow stages.

## Reporting procedure

At each gate or terminal status:

1. Write `<RUNTIME_REPORTS_DIR>/report.md` (create the directory if missing):

   ```
   ---
   type: gate | status
   name: <skill-specific marker>
   ---

   <body: the message the user needs to see, addressing the user directly>
   ```

2. Push the report directory to the lead:

   ```bash
   mngr push <lead_agent>:<lead_report_dir> \
       --source <RUNTIME_REPORTS_DIR>/ \
       --uncommitted-changes=merge
   ```

   Substitute the actual values from your task file's frontmatter for
   `<lead_agent>` and `<lead_report_dir>`. The trailing slashes matter (rsync
   directory semantics). `--uncommitted-changes=merge` is required because the
   lead's worktree usually has uncommitted local state.

3. Stop your turn. For gate reports, the lead sends the user's reply via
   `mngr message` and you resume; for terminal reports, the lead acts on the
   report and the run ends.

The push is the ready signal -- it only happens once you are finished writing.
Do not push a partial report.

## Terminal status report bodies

Each worker's SKILL.md lists which of these terminal statuses apply. The body
shapes are shared:

### `name: done`

```
Committed on branch `<branch-name>`. Ready to merge.
```

For verify-only flows (no new worker commits), substitute "Verified on branch
`<branch-name>`. Ready to merge." and optionally add: "No follow-up commits
needed; the substantive change is already on the branch from the live commit."

### `name: stuck`

A one-sentence reason and, if applicable, a recommendation for next steps:

```
I could not <do-the-task> because: <reason>. <optional: where work is, recommended next step>.
```

The flow-specific guidance for *when* to give up lives in each worker's
SKILL.md.

### `name: no-update-needed`

```
No update needed. Reason: <one-sentence>.
```

Do not commit a null change.
