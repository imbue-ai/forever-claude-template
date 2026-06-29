# Post-crystallize migration checklist

Run this after you have merged the worker's `mngr/crystallize-$NAME`
branch into your branch. The new skill is now installed
at `.agents/skills/<name>/`, but the calling-skill's runtime artifacts
(under `runtime/<calling-skill>/<slug>/`) and any code that referred
to them are still pointing at the temporary scratch shape. This doc
walks through the cleanup so nothing is left dangling.

The checklist is short on purpose. Stop reading once the items don't
apply -- it's possible nothing here is needed (e.g. a skill that no
consumer ever ran against the runtime path), and that's fine.

## 1. Find consumers that referenced the runtime fallback path

Look for code on the merged branch that referenced either:

- `runtime/fetch-process-show/<slug>/...`
- `runtime/<other-calling-skill>/<slug>/...`
- A symlink, env var, or hardcoded constant pointing at one of the above.

Use ripgrep (or your grep tool) with the slug as the search anchor:

```bash
rg -n "runtime/fetch-process-show/<slug>" -g '!runtime/'
rg -n "<slug>/fetch.py" -g '!runtime/'
```

For each hit, decide whether the consumer should switch to the
crystallized skill path. The standard switch is:

| Old reference                                                  | New reference                                            |
| -------------------------------------------------------------- | -------------------------------------------------------- |
| `runtime/fetch-process-show/<slug>/fetch.py`                     | `.agents/skills/<name>/scripts/run.py`                   |
| `runtime/fetch-process-show/<slug>/sample.json`                  | run the skill with `--output <path>` to regenerate       |
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

`runtime/harden/crystallize-<name>/` itself (the dir holding `task.md`,
`reports/`, and `ticket_id.txt`) is also stale post-merge,
but **do not delete it yet** -- section 5 below still needs to read
`ticket_id.txt`. Section 6's commit cleanup removes it after the
ticket is closed.

## 3. Reconcile shape changes the worker introduced

The worker is *encouraged* to improve the output shape during
crystallization -- it's the moment to reconsider how the task should
be done (see its Gate 2 "Shape changes from the sample" line and the
worker skill). So the merged skill's output may legitimately differ
from the sample the user confirmed; that's expected, not a defect.
Skim the worker's commits (`git log <merge-base>..HEAD`) and its Gate 2
summary for renames or semantic changes. Common kinds:

- **Field renames / restructuring** in the output JSON (e.g.
  `mention_count` → `channel_mention_count`, or a flat list becoming
  grouped).
- **Exit code changes** (e.g. tightening a generic non-zero into a
  documented `2 = auth missing`, `1 = other` split).
- **CLI flag renames** or default changes.

Two kinds of consumer to update:

- **Code consumers** keying on a field name or exit code -- the rename
  breaks them silently; grep for the old names and update.
- **Surfaces** built during `fetch-process-show`'s deliver phase (web
  views, etc.) that render the sample/pipeline output. Point them at the new output
  and update their rendering to the new shape.

**If the shape changed, re-confirm with the user.** They signed off on
the sample's shape, not the worker's revised one -- so after updating a
surface, show them the result (or at least describe what changed) and
let them react, rather than silently swapping in a different-looking
output.

## 4. Restart any service that consumed the old path

If a long-lived service (poller, watcher, daemon) imports the skill
or shells out to it, and that service was running before the merge,
it may have cached imports or be holding old subprocess paths. Restart
it so the new path takes effect:

- Supervisord-managed services (programs in `supervisord.conf`):
  `supervisorctl restart <name>` (or `stop` / `start`). Supervisord
  also restarts crashed `autorestart=true` services automatically, so
  killing the process bounces it onto the new code. If a program needs
  config changes first, edit `supervisord.conf` then
  `supervisorctl reread && supervisorctl update`.
- Subagents you started this session: send them a note via
  `mngr message <agent> -m "..."` if they need to pick up the change,
  or restart them.

## 5. Close the tracking ticket

`crystallize-artifact` Step 2 wrote the ticket ID to
`runtime/harden/crystallize-<name>/ticket_id.txt` at launch time. Read it and
close:

```bash
TICKET_FILE="runtime/harden/crystallize-<name>/ticket_id.txt"
if command -v tk >/dev/null 2>&1 && [ -s "$TICKET_FILE" ]; then
    tk close "$(cat "$TICKET_FILE")"
fi
```

If the file is missing (you deleted the runtime dir before
closing the ticket, or `tk` was unavailable at launch), `tk` does not
have a list command in this build -- look for the ticket in
`runtime/tickets/` by slug and close it by ID.

## 6. Commit the migration changes

The migration touches consumer code and removes runtime artifacts;
those should be a separate commit from the merge so the migration is
reviewable on its own. Now that section 5 has read `ticket_id.txt`,
also delete `runtime/harden/crystallize-<name>/` (unless the user asked to
keep the scratch around).

```bash
rm -rf runtime/harden/crystallize-<name>/
git add <consumer-files-you-changed>
git commit -m "post-crystallize migration for <slug>: switch consumers to skill path"
```

Then declare the crystallize done to the user with one short line
naming the skill, the one-line change in consumers, and the ticket
ID closed.
