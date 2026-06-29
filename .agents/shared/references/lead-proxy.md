# Lead-side proxy flow

Generic mechanics for driving a worker to completion and surfacing gate/status
reports to the user. The caller supplies flow-specific substitutions (worker
name, branch, runtime path, which gate names and terminal statuses apply).

## Polling for the next report

Start a background poll for the report file with `create_worker.py await`. It
reads `finish_report_path` from the task file's frontmatter, blocks until that
file appears, prints its contents, and exits 0; on timeout it exits non-zero
(code 124). Run it with Bash's `run_in_background: true` so it returns the
instant the report lands.

`await` is a generic poll-until-file primitive; the gate cycle below is this
flow's *use* of it. Non-interactive callers that launch a tightly-scoped agent
and wait for one finish report use the same `await` (or the synchronous
`create_worker.py launch-sync` wrapper) with no gate handling.

```bash
# Run with Bash run_in_background: true
uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --task-file <TASK_FILE>
```

`--timeout` defaults to `30m`; pass e.g. `--timeout 60m` to re-arm with a longer
wait. The tool output is the report contents: YAML frontmatter (`type`, `name`)
plus a body. If await exits non-zero (timeout) without printing a report, do
*not* immediately treat it as a terminal failure -- see "Diagnose worker
liveness" below.

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
  intent, scope, or domain that you cannot make from context). Most
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

## `mngr rsync` rationale

When syncing reports (or the initial runtime dir to the worker):

```bash
mngr rsync ./<SOURCE_DIR>/ <WORKER>:<DEST_DIR>/ \
    --uncommitted-changes=merge
```

- `mngr rsync` takes `SOURCE DESTINATION` (positional): the local source dir
  first, then the `<WORKER>:<PATH>` agent endpoint. Exactly one side must
  reference an agent or remote host.
- Path resolution: mngr treats an argument as a *local path* only when it
  starts with `/`, `./`, `../`, or `~/` -- a bare `runtime/foo` is read as an
  *agent name* (hence the `./` on the source above). On an agent endpoint, a
  relative `<WORKER>:PATH` resolves against the worker's workdir; an absolute
  `<WORKER>:/PATH` is used verbatim.
- Use the directory form (trailing slash on both sides). mngr passes the paths
  through to rsync verbatim, so the trailing slash is load-bearing: it makes
  rsync copy directory *contents* into the destination instead of nesting the
  dir under it. Syncing a single file fails -- rsync wants a directory.
- `--uncommitted-changes=merge` is required. The worker's worktree has
  uncommitted changes immediately after creation (the installed worker
  sub-skills under `.agents/skills/`), so the default `fail` mode would refuse
  the sync.
- There is no `mngr file put` subcommand -- `mngr rsync` is the correct
  mechanism.
