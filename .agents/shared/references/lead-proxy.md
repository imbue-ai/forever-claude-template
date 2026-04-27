# Lead-side proxy flow

Shared across `crystallize-task`, `heal-skill`, and `update-skill`: the three
lifecycle skills use the same file-based protocol to drive their worker to
completion and surface gate/status reports to the user.

This file covers the generic mechanics. Each caller supplies its own
flow-specific substitutions (worker name, branch, runtime path, which gate
names and terminal statuses apply).

## Polling for the next report

Start a background poll for the report file. Bash's `run_in_background: true`
returns the instant the file appears.

```bash
# Run with Bash run_in_background: true
timeout 30m bash -c '
  while [ ! -f <REPORTS_DIR>/report.md ]; do sleep 5; done
  cat <REPORTS_DIR>/report.md
'
```

The tool output is the report contents: YAML frontmatter (`type`, `name`) plus a
body. If the timeout trips without the file appearing, do *not* immediately
treat it as a terminal failure -- see "Diagnose worker liveness" below.

## Diagnose worker liveness before invoking failure flow

If the timeout trips without a report appearing, the worker may still be
alive and working. Long-running stages (autofix, verify-architecture, large
implementations) can legitimately exceed 30 minutes on a healthy worker.
Before invoking the failure flow, check the worker session:

```bash
tmux capture-pane -t minds-<WORKER_NAME>:claude -p -S -100 | tail -40
```

If the output shows ongoing tool use, an active spinner / "Running…" line,
or recent timestamps within the last few minutes, the worker is alive --
re-arm the poll with a longer timeout (e.g. `60m`) and continue. Only
invoke the failure flow (`.agents/skills/launch-task/references/worker-failure.md`)
if the session is dead, the agent is wedged on the same operation for an
extended period, or output has been static.

## Do not interrupt more recent user work

If the user gave you a more recent task since launching the worker, finish that
task first. The report notification is informational -- act on it once the
user's current request is complete.

## Parsing the report

Parse the YAML frontmatter: `type` (`gate` or `status`) and `name`
(skill-specific). The body is the message the user needs to see.

If the file does not parse (no frontmatter, unknown type, truncated), treat it
as a terminal failure.

## Deciding: answer gate yourself vs. escalate

On `type: gate`:

- **Answer yourself** for implementation details: script structure, naming
  conventions, which utility to reuse, file layout, agentskills.io compliance,
  or anything you can determine from reading files or applying the calling
  skill's own guidelines. The user does not care about technical details --
  do not surface them.
- **Escalate to the user** for user intent, scope, subjective preference, or
  domain knowledge you do not have. `final-artifact` gates always escalate.
  `outline-approval` gates default to answer-yourself; only escalate if the
  worker has surfaced a *genuine process question* (a decision about user
  intent, scope, or domain that the lead cannot make from context). Most
  outline gates do not contain such questions and should not be forwarded.
- **Mix**: if a gate bundles an approval (escalate) with implementation
  sub-questions, pre-answer the sub-questions in the message you forward to the
  user so they do not have to weigh in on them.

The worker is framed as addressing the user directly. When you answer, write
your reply in the user's voice and forward via `mngr message`:

```bash
mngr message <WORKER_NAME> -m "<reply, in the user's voice>"
```

To escalate, use the `send-user-message` skill on your own channel, wait for
the user's reply, then forward it via `mngr message`.

After forwarding, consume the report so the next push can land a fresh
`report.md`:

```bash
mkdir -p <REPORTS_DIR>/consumed
mv <REPORTS_DIR>/report.md <REPORTS_DIR>/consumed/$(date +%s)-gate.md
```

Then re-arm the background poll.

## Terminal status: act and stop polling

On `type: status`:

- `name: done` -- merge the worker's branch:
  ```bash
  git fetch . <WORKER_BRANCH>:<WORKER_BRANCH>
  git merge --no-ff <WORKER_BRANCH>
  ```
  If the merge conflicts, resolve manually. On successful merge, close any
  tracking ticket and optionally destroy the worker.

- `name: stuck`, or the 30m timeout tripped without a report arriving -- follow
  `.agents/skills/launch-task/references/worker-failure.md`: surface the report
  body (or its absence) to the user, point at the branch and worker agent, and
  leave both intact for manual inspection.

- `name: no-update-needed` (or other skill-specific benign no-op terminals) --
  the worker decided there was nothing to do. Close any tracking ticket and
  stop; do not merge, do not invoke the failure flow. Optionally surface the
  one-sentence reason to the user.

In every status case, consume the report (move to `<REPORTS_DIR>/consumed/`) so
the directory is clean for future runs.

## `mngr push` rationale

When pushing reports (or the initial runtime dir to the worker):

```bash
mngr push <WORKER>:<DEST_DIR> \
    --source <SOURCE_DIR> \
    --uncommitted-changes=merge
```

- Use the directory form (trailing slash on both sides). Pushing a single file
  via `--source .../file.txt` fails: rsync interprets the source as a directory
  and errors with `change_dir`.
- `--uncommitted-changes=merge` is required. The worker's worktree has
  uncommitted changes immediately after creation (the installed worker
  sub-skills under `.agents/skills/`), so the default `fail` mode would refuse
  the push.
- There is no `mngr file put` subcommand -- `mngr push` is the correct
  mechanism.

## `extract_turn.py`

Capture a turn from the current session transcript:

```bash
uv run .agents/shared/scripts/extract_turn.py \
    --nth 1 \
    --output <RUNTIME_DIR>/turn.jsonl
```

`--nth 1` selects the *previous* human turn -- the one you typically want to
capture. `--nth 0` (the default) would capture the current invocation turn,
which is rarely what you want.

For marker-based slicing when turn counts do not line up cleanly (e.g.
sub-agent interleaving) and the transcript path resolution chain, run
`uv run .agents/shared/scripts/extract_turn.py --help`.
