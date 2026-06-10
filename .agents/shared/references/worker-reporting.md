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
(and whose transcript you read); `FINISH_REPORT_PATH` is the destination
path on the lead's worktree where your report file must land -- the lead
polls for exactly this file. Any additional string fields the lead
set in the frontmatter also become shell variables -- see your worker
SKILL.md for which extras (if any) the calling flow stages.

## Reporting procedure

At each gate or terminal status:

1. Write your report to `<RUNTIME_REPORTS_DIR>/report.md` (create the directory
   if missing). `report.md` is the basename of `FINISH_REPORT_PATH`, so pushing
   the directory in step 2 lands it at the lead's `FINISH_REPORT_PATH`.

   ```
   ---
   type: gate | status
   name: <skill-specific marker>
   ---

   <body: the message the user needs to see, addressing the user directly>
   ```

2. Sync the report directory to the lead:

   ```bash
   mngr rsync ./<RUNTIME_REPORTS_DIR>/ \
       "$LEAD_AGENT:$(dirname "$FINISH_REPORT_PATH")/" \
       --uncommitted-changes=merge
   ```

   `mngr rsync` takes `SOURCE DESTINATION`: your local `<RUNTIME_REPORTS_DIR>/`
   first, then the lead endpoint. `LEAD_AGENT` / `FINISH_REPORT_PATH` come from
   the `eval` above; `<RUNTIME_REPORTS_DIR>` is your worker SKILL.md's local
   reports dir. mngr treats an argument as a local path only when it starts with
   `/`, `./`, `../`, or `~/` (hence the `./` on the source; a bare `runtime/foo`
   reads as an agent name), and a relative path on the lead endpoint resolves
   against the lead's workdir. You sync the report's *parent directory*
   (`dirname`) rather than the file itself: the trailing slashes matter (rsync
   directory semantics) and rsync cannot transfer a single file.
   `--uncommitted-changes=merge` is required because the lead's worktree usually
   has uncommitted local state.

3. Stop your turn. For gate reports, the lead sends the user's reply via
   `mngr message` and you resume; for terminal reports, the lead acts on the
   report and the run ends.

The sync is the ready signal -- it only happens once you are finished writing.
Do not sync a partial report.

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
