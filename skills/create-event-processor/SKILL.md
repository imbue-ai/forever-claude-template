---
name: create-event-processor
description: Create a persistent sub-agent that stays alive and processes events. Use when you need a background watcher that reacts to events, polls a service, or runs continuously.
---

# Creating an event processor

An event processor is a persistent sub-agent that stays alive indefinitely, processing events as they arrive. It uses a stop hook to prevent exit and an idle backoff to avoid wasting resources.

The `events_processor/` directory in this repo is pre-configured with the stop hook, wait script, and Claude hooks.

## 1. Write the purpose

Before creating the event processor, write its PURPOSE.md describing what it should do:

```bash
cat > events_processor/PURPOSE.md << 'PURPOSE_EOF'
<Describe what events to watch for and what to do with them.
For example: "Poll the GitHub API every 5 minutes for new issues
and send a summary to the parent agent via mngr message.">
PURPOSE_EOF
```

Commit this change so the event processor's repo has it:

```bash
git add events_processor/PURPOSE.md && git commit -m "Configure event processor purpose"
```

## 2. Create the sub-agent

```bash
mngr create <name> --type claude \
    --transfer none \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message "Read PURPOSE.md and begin executing on your purpose."
```

The `--transfer none` flag means the agent runs directly in the `events_processor/` directory rather than copying files.

## 3. Monitor

The event processor stays alive and processes events. You can check on it:

```bash
mngr capture <name>          # see current terminal output
mngr transcript <name>       # read conversation history
mngr message <name> -m "..."  # send it a message
```

## How it works

The event processor has:
- A stop hook (`scripts/stop_hook.sh`) that always prevents exit (exit code 2)
- A wait script (`scripts/wait.sh`) with increasing backoff: [1, 1, 5, 10, 30, 60] minutes
- Claude hooks that reset the backoff when a real message arrives
- Its own CLAUDE.md with minimal instructions for persistent operation
