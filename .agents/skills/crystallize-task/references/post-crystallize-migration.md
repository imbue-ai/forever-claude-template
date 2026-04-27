# Post-crystallize migration checklist

Run this after the lead has merged a worker's `mngr/crystallize-$NAME`
branch into the calling agent's branch. The new skill is now installed
at `.agents/skills/<name>/`, but the calling-skill's runtime artifacts
(under `runtime/<calling-skill>/<slug>/`) and any code that referred
to them are still pointing at the temporary scratch shape. This doc
walks through the cleanup so nothing is left dangling.

The checklist is short on purpose. Stop reading once the items don't
apply -- it's possible nothing here is needed (e.g. a skill that no
consumer ever ran against the runtime path), and that's fine.

## 1. Find consumers that referenced the runtime fallback path

Look for code on the merged branch that referenced either:

- `runtime/do-something-new/<slug>/...`
- `runtime/<other-calling-skill>/<slug>/...`
- A symlink, env var, or hardcoded constant pointing at one of the above.

Use ripgrep (or your grep tool) with the slug as the search anchor:

```bash
rg -n "runtime/do-something-new/<slug>" -- '!.tickets' '!runtime'
rg -n "<slug>/fetch.py" -- '!.tickets' '!runtime'
```

For each hit, decide whether the consumer should switch to the
crystallized skill path. The standard switch is:

| Old reference                                                  | New reference                                            |
| -------------------------------------------------------------- | -------------------------------------------------------- |
| `runtime/do-something-new/<slug>/fetch.py`                     | `.agents/skills/<name>/scripts/run.py`                   |
| `runtime/do-something-new/<slug>/sample.json`                  | run the skill with `--output <path>` to regenerate       |
| Inline import of the fetch script                              | `subprocess.run(["uv", "run", "python", "<skill-path>"])`|

If a consumer has explicit fallback logic (e.g. "use the skill if
installed, else use the runtime path"), that fallback can stay until
the next refactor pass -- it's defensive code, not a bug. Note in the
PR description that the fallback is now dead code.

## 2. Decide whether to delete the runtime artifact directory

Default: delete `runtime/<calling-skill>/<slug>/`. The skill is the
canonical source going forward; if the user wants fresh sample data,
they re-run the skill.

```bash
rm -rf runtime/<calling-skill>/<slug>/
```

Skip the delete if any of the following:
- A consumer's fallback logic is still live and needs the file.
- The user explicitly asked to keep the live scratch around for
  debugging or comparison.
- The directory contains user-supplied state (auth tokens, captured
  inputs you'd lose) -- read it first to be sure.

## 3. Note breaking changes the worker introduced

Skim the worker's commits (`git log <merge-base>..HEAD`) for renames
or semantic changes the worker landed during its autofix loop that
weren't in the original sample. Common kinds:

- **Field renames** in the output JSON (e.g. `mention_count` →
  `channel_mention_count` because the original name was misleading).
- **Exit code changes** (e.g. tightening a generic non-zero into a
  documented `2 = auth missing`, `1 = other` split).
- **CLI flag renames** or default changes.

Update consumers to match the new contract. If a consumer was already
keying on a field name or exit code, the rename will break it
silently -- grep for the old names and update.

## 4. Restart any service that consumed the old path

If a long-lived service (poller, watcher, daemon) imports the skill
or shells out to it, and that service was running before the merge,
it may have cached imports or be holding old subprocess paths. Restart
it so the new path takes effect:

- `services.toml`-managed services: kill the wrapper process; the
  bootstrap manager restarts on `restart = "on-failure"`. If it
  doesn't (see CLAUDE.md cwd / bootstrap notes), restart manually
  with `uv run <command>` from the repo root.
- Subagents you started this session: send them a note via
  `mngr message <agent> -m "..."` if they need to pick up the change,
  or restart them.

## 5. Close the tracking ticket

```bash
if command -v tk >/dev/null 2>&1 && [ -n "${TICKET_ID:-}" ]; then
    tk close "$TICKET_ID"
fi
```

If `$TICKET_ID` was lost across the lead's context (long crystallize
runs sometimes outlive a lead's working memory), `tk` does not have a
list command in this build -- look for the ticket in `.tickets/` by
slug and close it by ID.

## 6. Commit the migration changes

The migration touches consumer code and removes runtime artifacts;
those should be a separate commit from the merge so the migration is
reviewable on its own.

```bash
git add <consumer-files-you-changed>
git commit -m "post-crystallize migration for <slug>: switch consumers to skill path"
```

Then declare the crystallize done to the user with one short line
naming the skill, the one-line change in consumers, and the ticket
ID closed.
