# Worker reporting contract

Generic file-based protocol for signaling the lead at each gate and at
terminal status. The worker supplies flow-specific runtime paths and the
enum of allowed `name:` values.

## Task-file inputs

Your task file has been synced to your worktree alongside the replay artifact
(`turn.jsonl` for `absorb`, `crystallize`, and `heal` flows; `commit.diff` for
the `verify` flow) at `<RUNTIME_DIR>/task.md`. At the start of your run,
validate its frontmatter and extract the three required fields with:

```bash
uv run .agents/shared/scripts/parse_task_frontmatter.py '<TASK_FILE_GLOB>'
```

Quote the glob pattern so the shell passes the literal to the helper; the
helper expands it internally and fails loudly if zero or more than one task
file matches (each worker handles a single task -- either condition means the
runtime layout drifted). On success it prints three shell-evalable `KEY=value`
lines on stdout: `LEAD_AGENT=`, `LEAD_REPORT_DIR=`, `TRANSCRIPT_PATH=`. It
exits non-zero with a stderr message on any failure, including a missing or
misspelled field or a non-string / empty value.

The first two address reports back to the lead; `transcript_path` is where the
replay artifact lives.

## Task-file frontmatter schema

```yaml
---
lead_agent: <main agent name>
lead_report_dir: runtime/<flow>/<name>/reports/
transcript_path: runtime/<flow>/<name>/turn.jsonl
---
```

All three fields are required non-empty strings. `parse_task_frontmatter.py`
enforces this.

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
