---
name: launch-task
description: Create a sub-agent to perform a larger task. Use when work is large enough to warrant a separate context, involves multi-file changes, or benefits from isolation.
---

# Launching a task

## 1. Write a task description

Write a clear task file describing what needs to be done:

```bash
cat > /tmp/task-<name>.md << 'TASK_EOF'
# Task: <title>

## What to do
<description of what needs to be done and why>

## Context
<any relevant context: file paths, prior attempts, constraints>

## Success criteria
<what "done" looks like -- be specific>
TASK_EOF
```

## 2. Create the sub-agent

```bash
mngr create <task-name> -t worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-<name>.md
```

The `worker` template automatically configures the agent with:
- `--dangerously-skip-permissions`
- Code review (imbue-code-guardian) settings
- A system prompt telling it to commit changes and not ask unnecessary questions

Task names must be unique (git branches are created). Use descriptive names like `fix-login-bug` or `add-search-feature`.

## 3. Wait for completion (background)

Start `mngr wait` in the background (using the Bash tool with
`run_in_background: true`) so you can continue working. You will be
notified when it completes -- do not block on it.

```bash
# Run with Bash run_in_background: true
mngr wait <task-name> DONE STOPPED WAITING --timeout 30m
```

This will notify you when the agent reaches one of those terminal states.

## 4. Check results

When the wait completes, check what happened:

```bash
# See current state
mngr list --label workspace=$MINDS_WORKSPACE_NAME --format jsonl

# Read the agent's conversation
mngr transcript <task-name> --role=user --role=assistant | tail -n 30

# See what's on screen right now
mngr capture <task-name>
```

## 5. Handle the outcome

**Agent finished (DONE/STOPPED):**
- Check the transcript for any questions the agent had (look at the last few assistant messages)
- If the code review (autofix) ran, the agent likely finished successfully
- Results are on a git branch (`mngr/<task-name>`), accessible via `git log mngr/<task-name>`
- Optionally destroy: `mngr destroy <task-name>`

**Agent is WAITING:**
- Check if the code reviewer ran and the agent is waiting for permission -- in this case it likely finished
- Look at earlier assistant messages to see if the agent asked a question
- If it asked a question, answer via `mngr message <task-name> -m "your answer"`
- If it seems stuck, check `mngr capture <task-name>` for dialog boxes or errors

## Worktree isolation and gitignored files

Each sub-agent is created in its own git worktree of the current repo
(see `worktree_base_folder` in `.mngr/settings.toml`). The worktree is a
fresh checkout of the branch, so the worker sees committed files but
**does not** see anything under `.gitignore` — including `runtime/`,
`memory/`, scratch files in `/tmp`, and uncommitted/untracked files in
your own working directory.

If the worker needs access to a gitignored file (e.g. a transcript
dumped under `runtime/`, a dataset, a credential file), push it into
the worker's working directory after `mngr create` with `mngr push`:

```bash
# Push a specific directory to the same relative path in the worker
mngr push <task-name>:path/to/dir path/to/dir

# Push a single file to a specific target path
mngr push <task-name>:path/to/file.txt path/to/file.txt
```

`mngr push` uses rsync by default and only modifies the target
workspace. Do it before the worker reaches the step that needs the
file — in practice, immediately after `mngr create`, since worker
startup (provisioning + loading skills) typically gives you a window
before the task message is acted on. See `mngr push --help` for full
options.

Committed files are already present in the worker's worktree; you do
not need to push those.

## Guidelines

- Always include clear success criteria in your task description
- Use `mngr wait` in the background -- don't block yourself waiting for a task
- Check the transcript when a task finishes to see if the agent had questions or concerns
- If the task references gitignored files, push them with `mngr push` right after `mngr create` (see above)
- If a task fails, see `references/worker-failure.md` for how to capture context and report to the user (do not retry silently).
