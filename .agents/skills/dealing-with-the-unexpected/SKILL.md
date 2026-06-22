---
name: dealing-with-the-unexpected
description: Handle unexpected situations where things are not working as expected. Use when you encounter errors, confusing state, or behavior that contradicts your docs and prompts.
---

# Dealing with the unexpected

When something unexpected happens, follow this procedure:

## 1. Gather information

Before doing anything, understand what is actually happening:

- Read any error messages carefully
- Check the tmux windows: `tmux list-windows -t $(tmux display-message -p '#S')`
- Check if services are running: `supervisorctl status` (compare against the programs defined in `supervisord.conf`)
- If the deployment uses telegram, check recent history: `tail -20 runtime/telegram/history.jsonl`
- Check the wait counter state: `cat runtime/wait_counter 2>/dev/null || echo "no counter"`

## 2. Diagnose

Common issues and their causes:

- **User-messaging channel broken** (telegram, etc.): if the configured
  channel is not delivering, check that channel's tmux window for errors and
  confirm the relevant env vars are set. See the `send-user-message` skill
  for how channel selection works; fall back to inline responses until the
  channel is restored.
- **Services not starting**: Run `supervisorctl status` and read the service's logs under `/var/log/supervisor/<name>-stderr.log` (or `supervisorctl tail -f <name> stderr`). The `bootstrap` tmux window shows supervisord's own output. Verify `supervisord.conf` is valid, then `supervisorctl reread && supervisorctl update`.
- **Wait script behaving oddly**: Check `runtime/wait_counter` contents. Delete it to reset: `rm -f runtime/wait_counter`

## 3. Fix or escalate

If you can fix the issue yourself (edit a config, restart a service, etc.), do so and commit the fix.

If you cannot fix the issue, tell the user via `send-user-message`:
- What you observed
- What you tried
- What you think the problem might be
- Ask for their help

## 4. Never panic

You are a persistent agent. Even if something is broken, you will keep running. Focus on what you can do, communicate clearly with the user, and wait for help if needed.
