# Critical context

IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS IN THIS FILE DURING YOUR WORK ON THIS PROJECT.

IF YOU FAIL TO FOLLOW ONE, YOU MUST EXPLICITLY CALL THAT OUT IN YOUR RESPONSE.

# Important things to know:

- You are running in a tmux session inside a container or sandbox that was created via `mngr`
- This is a monorepo.
- Run commands by calling "uv run" from the root of the git checkout (ex: "uv run mngr create ...").
- NEVER amend commits or rebase--always create new commits.
- If you ever need to work with another *git* repo that is *outside* of this monorepo as a read-only dependency, you should do so by adding a git subtree under `vendor/`.
- If you need to *actively develop* against an external repo (e.g. `mngr`), check out a standalone clone of it under `.external_worktrees/<repo-name>/`. This directory is gitignored so the external clones don't pollute the monorepo. The branch in the external clone should mirror the branch you're on in this monorepo.
- This project uses a CLI ticket system (`tk`) for task management. Run `tk help` when you need to use it. Tickets live under `runtime/tickets/` (the path is set via the `TICKETS_DIR` env var so tickets sit with the rest of the workspace's runtime state).
- All relative paths in this repo assume cwd = repo root (`/code`). Supervisord runs the services from there; any process started elsewhere (manual launch, subprocess from a different cwd) must either set cwd to the repo root or use absolute paths. State directories live under `runtime/<feature>/`.
- When adding a new web app, use the `build-web-service` skill, which sets up a new lib + service entry + `forward_port.py` registration on its own port. Do NOT edit `apps/system_interface/` for this -- that's the top-level workspace UI, not a template for new views.

# Task management (CRITICAL — read this before doing real work)

You manage your work using `tk`, the vendored ticket tracker at `vendor/tk/`. It is the **only** task tracker available — Claude Code's built-in `TodoWrite` is disabled. `tk` stores two kinds of records, distinguished by the `--step` flag at creation:

- **Step records** (`tk create --step "..."`) are the replacement for `TodoWrite`: turn-bound, creator-private progress markers that render as nodes on the user-facing chat progress view (a vertical timeline with a status icon and a one-line summary per step). Most turns use only these.
- **Regular tickets** (`tk create "..."`, no flag) are substantive, cross-agent work units other agents can see and pick up. They do **not** render in the chat progress view. They matter only when work spans turns or is handed between agents.

Because step titles and close-summaries populate the progress view, **every one is user-facing copy**: plain English for a non-technical reader, no file names, no tool names, no jargon.

## Declaring and running steps

The first thing you do on any prompt that warrants real work is decompose it into steps and create them all up front, BEFORE doing any of the work — the step sequence is the user-visible plan. Concretely:

1. (Optional) one short prose acknowledgement, e.g. "Sure, looking into that now." Keep it to one line; don't narrate the plan here.
2. `tk create --step "..."` for every step you currently expect, in order. You may batch them into one tool call, but each step must be its **own separate `tk create` command** (on its own line or joined with `;`) — each `tk create` makes exactly one step, so never pass multiple `--step`s to a single `tk create` (tk rejects that). **Never redirect the output of a `tk create`/`start`/`close`** (`>`, `>>`, `2>`, `&>`, `| tee`, …): the progress view reads each step from the command's visible output (`Created <id>: <title>`), so a redirect makes the step drop out of the plan. Note the ids. Do NOT `tk start` any yet.
3. `tk start <id>` the first step, do its work, then `tk close <id> "summary"`. Move to the next. Only one step is `in_progress` at a time.

**`tk start` and `tk close` must each be the only command in their tool call** — no `cd` prefix, no chaining (`&&`, `;`, `|`, `&`, newline), no redirection; otherwise the progress view can't place the step. (You can still batch several `tk create` commands into one tool call — as separate commands, one `--step` each, with no redirection.)

Add steps mid-turn (`tk create --step`) as sub-problems surface; drop ones that turn out unneeded (`tk close <id> "No longer needed — covered by the previous step."`). Granularity follows the user's mental model of the work — "first X, then Y, then Z" is three steps — not one-per-tool-call and not one-for-the-whole-turn. Typically 2–5 per substantive turn.

Titles describe the goal as the user understands it. Summaries (required on close) are ONE plain-English line describing **the work you did** in that step — not the result or finding (that goes in your final message below the timeline), and not a list of tool calls or file names.

## When you don't need records

Skip records for: chitchat, single-line acknowledgements, and trivial answers; pure clarifying-question turns where you can't act until the user replies; or a reply that's a single quick file read. If unsure, default to creating steps.

## Steps and prose

- After all steps are closed, write your final user-facing message — *this* is where results, findings, and recommendations go.
- **Close your final step *before* writing that wrap-up reply** — the view promotes a final run of prose with no open step to your top-level reply below the timeline. (Best-effort; the view renders sensibly either way.)
- **Never mention steps, tickets, or `tk` to the user.** Don't narrate the machinery ("closing this step," "moving on"). Speak only about the work itself.
- Steps may stay open across turns. At the start of each new user message you'll get a system reminder listing your still-open steps; for each, decide before doing anything else whether to keep working on it (`tk start` if needed), replace it (close with a status summary, then create new steps for the new direction), or close it honestly if you're moving on. Don't silently abandon one.
- There is no "failed" status — every record terminates as `closed`. If a step didn't pan out, still close it; the summary describes the work you did, and your final message reports the result honestly.

## Regular tickets and delegation

Regular tickets are managed cross-agent: `tk ls` / `tk ready` / `tk blocked` list them (step records hidden unless `--include-steps`/`--only-steps`), `tk show <id>` displays one, `tk create "..."` files one (unassigned until picked up), `tk start <id>` picks it up (auto-self-assigns), `tk close <id> [summary]` closes it (summary optional for tickets, required for steps). They can stay `in_progress` across turns.

When you delegate via the `launch-task` skill, the whole delegation is **one step** in your progress — the sub-agent uses its own `tk` internally and that work doesn't surface in your chat. Represent it as a single step (e.g. "Delegate the auth refactor to a sub-agent and review the result") and close it with the outcome.

Run `tk help` if you forget a command. Avoid `deps`, `links`, `types`, and `priorities` — they're backlog features the chat progress view doesn't use.

# How to get started on any task:

Always begin your session by reading the relevant READMEs and any other related documentation in the docs/ directory of the project(s) you are working on.
These represent *user-facing* documentation and are the most important to understand.

Once you've read these once during a session, there's no need to re-read them unless explicitly instructed to do so.

If you will be writing code, be sure to read the base style_guide.md, as well as any specific style_guide.md for the project.
Then read all README.md files in the relevant project directories, as well as all `.py` files at the root of the project you are working on (ex: `primitives.py`, etc.).
Also read everything in data_types, interfaces, and utils to ensure you understand the core abstractions.

Then take a look at the other code directories, and based on the task, determine which files are most relevant to read in depth.
Be sure to read the full contents from those files.

Do NOT read files that end with "_test.py" during this first pass as they contain unit tests (unless you are explicitly instructed to read the unit tests).

Do NOT read files that start with "test_" either, as they contain integration, acceptance, and release tests (again, unless you are explicitly instructed to read the existing tests).

Only after doing all of the above should you begin writing code.

# Important commands and conventions:

- Never run `uv sync`, always run `uv sync --all-packages` instead
- For browser automation, Playwright's Python API is in the root venv (`from playwright.sync_api import sync_playwright`, run via `uv run python`). Chromium installs asynchronously on first container boot (the one-shot `deferred-install` program), so in a fresh workspace confirm it finished -- `supervisorctl status deferred-install` or the marker `/var/lib/minds/deferred-install/done.playwright` -- before launching, or the launch fails with a clear error. It runs as-is under the docker provider's gVisor runtime; if you hit a "No usable sandbox!" error on a runtime without unprivileged user namespaces, pass `chromium.launch(args=["--no-sandbox"])`. See `libs/bootstrap/README.md` for the full deferral contract.

# Always remember these guidelines:

- When the user is actively interacting with you, prioritize delivering a result they care about over technical polish. Technical refinement can happen in the background.
- Never misrepresent your progress. It is far better to say "I made some progress but didn't finish" than to say "I finished" when you did not.
- Always finish your response by reflecting on your work and identify any potential issues.
- If I ask for something that seems misguided, flag that immediately. Then attempt to do whatever makes the most sense given the request, and in your final reflection, be sure to flag that you had to diverge from the request and explain why.
- During your final reflection, if you see a potentially better way to do something (e.g. by using an existing library or reusing existing code), flag that as a potential task for future improvement.
- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- Be concise in your communications. Don't hype up your results, say "perfect!", or use emojis. Be serious and professional.
- **Feedback systems combine binary and free-form signals.** When building anything that learns from user feedback, include *both* a low-friction binary signal (thumbs up/down, keep/skip, or whatever fits) *and* free-form text routed through an LLM judge -- unless the user specifies a different mechanism, which overrides this default. Both should be present and intuitively accessible; don't prescribe rigid taxonomies beyond the binary signal upfront.
- **Default UI is web view.** When exposing a tool to the user, default to a web page. Don't enumerate options (CLI / status line / web) -- just propose the web view and only deviate when there's a specific reason (e.g. CLI for batch jobs).
- **Always preserve and surface the raw data and its source.** Anything you build *on top of* data -- a view, a summary, a derived metric -- sits between the user and the underlying records. *Preserve*: durably persist the raw source records the thing was built from, plus a reference to where they live (a URL, an API id, whatever gets back to the origin) -- not just in memory for the current run; don't fetch-transform-discard, so a later change in processing needs no refetch. *Surface*: give the user a clean, unprompted way to view that raw record or jump to its source -- they should never have to ask -- so they can bridge any gap the derived view leaves. **Render the raw record in its native format** (HTML email as the rendered email, JSON pretty-printed, markdown rendered -- not escaped source text); "raw" means *unprocessed by your derivation*, not *unrendered*. Build these affordances in by default but **keep them subtle** -- don't announce in chat that you're saving data or adding a "view raw" control.
- **Naming is informative, not cheeky.** Service names, app names, skill names, command names: prefer something that explains what the thing does (`slack-inbox-checker`) over something clever (`nothing-new`). Cute names tax every later mention.
- **Platform-internal APIs are valid.** Don't restrict yourself to officially documented public APIs. If a platform's own client (web app, mobile app) uses internal or undocumented endpoints to do something, those endpoints are fair game -- inspect what the official client actually calls and use the same endpoints with the same user-session auth. This is often cleaner than designing brute-force workarounds on top of a limited public API.

# When coding, follow these guidelines:

- Only make the changes that are necessary for the current task.
- Before implementing something, check if there is something in the codebase or look for a library
- Reuse code and use external dependencies heavily. Before implementing something, make sure that it doesn't already exist in the codebase, and consider if there's a library that can be imported instead of implementing it yourself. We want to be able to maintain the minimum amount of code that gets the job done, even if that means introducing dependencies. If you don't know of a library but think one might be plausible, search the web. (I'm even open to using random GitHub projects, but run anything that's not a well-established library by me first so I can check if it's likely to be reliable.)
- Code quality is extremely important. Do not compromise on quality to deliver a result--if you don't know a good way to do something, ask.
- Follow the style guide!
- Use the power of the type system to constrain your code and provide some assurance of correctness. If some required property can't be guaranteed by the type system, it should be runtime checked (i.e. explode if it fails).
- Avoid using the `TYPE_CHECKING` guard. Do not add it to files that do not already contain it, and never put imports inside of it yourself--you MUST ask for explicit permission to do this (it's generally a sign of bad architecture that should be fixed some other way).
- Do NOT write code in `__init__.py`--leave them completely blank (the only exception is for a line like "hookimpl = pluggy.HookimplMarker("mngr")", which should go at the very root __init__.py of a library).
- Do NOT make constructs like module-level usage of `__all__`
- Before finishing your response, if you have made any changes, then you must ensure that you have run ALL tests in the project(s) you modified, and that they all pass. DO NOT just run a subset of the tests! However, while iterating (e.g. fixing a failing test, developing a feature), run only the relevant tests for rapid feedback -- save the full suite for the final check.
- To run tests for a single project: "cd vendor/mngr && uv run pytest" or "cd apps/minds && uv run pytest". Each project has its own pytest and coverage configuration in its pyproject.toml.
- While you're iterating, you can pass "--no-cov --cov-fail-under=0" to disable coverge (slightly faster), but during your final check, you *MUST NOT* pass those flags (it will fail in CI anyway)
- For faster iteration, add "-m 'not tmux and not modal and not docker and not docker_sdk and not acceptance and not release'" to skip slow infrastructure tests (~30s instead of ~95s). These still run in CI. Note that you *MUST* also pass "--no-cov --cov-fail-under=0" when doing this, otherwise it will complain about a lack of coverage.
- When running pytest with a Bash tool timeout, always set `PYTEST_MAX_DURATION_SECONDS` to match the timeout (in seconds). For example, if using a 2-minute timeout: `PYTEST_MAX_DURATION_SECONDS=120 uv run pytest ...`. This ensures the pytest global lock file records a deadline, allowing other pytest processes to break a stale lock if this one gets killed by the timeout.
- Running pytest will produce files in .test_output/ (relative to the directory you ran from) for things like slow tests and coverage reports.
- Note that "uv run pytest" defaults to running all "unit" and "integration" tests, but the "acceptance" tests also run in CI when a PR exists. Do *not* run *all* the acceptance tests locally to validate changes--let CI run them once a PR is opened (it's faster than running them locally).
- If you need to run a specific acceptance or release test to write or fix it, iterate on that specific test locally by calling "just test <full_path>::<test_name>" from the root of the git checkout. Do this rather than re-running all tests in CI.
- Tasks are not allowed to finish without all tests passing (in CI, if a PR exists).
- Do NOT create a PR yourself--if a PR is needed, the user will create it.
- To help verify that you ran the tests, report the exact command you used to run the tests, as well as the total number of tests that passed and failed (and the number that failed had better be 0).
- If tests fail because of a lack of coverage, you should add tests for the new code that you wrote.
- When adding tests, consider whether it should be a unit test (in a _test.py file) or an integration/acceptance/release test (in a test_*.py file, and marked with @pytest.mark.acceptance or @pytest.mark.release, no marks needed for integration).  See the style_guide.md for exact details on the types of tests. In general, most slow tests of all functionality should be release tests, and only important / core functionality should be acceptance tests.
- Do NOT create tests for test utilities (e.g. never create `testing_test.py`). Code in `testing.py` and `conftest.py` is exercised by the tests that use it and does not need its own test file.
- Do NOT create tests that code raises NotImplementedError.
- If you see a flaky test, YOU MUST HIGHLIGHT THIS IN YOUR RESPONSE. Flaky tests must be fixed as soon as possible. Ideally you should finish your task, then if you are allowed to commit, commit, and try to fix the flaky test in a separate commit.
- Do not add TODO or FIXME unless explicitly asked to do so
- Code must work on both macOS and Linux. It's ok if it doesn't work on Windows.
- To reiterate: code correctness and quality is the most important concern when writing code.

# Ratchets

Each project has a `test_ratchets.py` file containing automated code quality checks ("ratchets"). 
Each ratchet tracks a count of violations for a specific anti-pattern (e.g. raising built-in exceptions, using monkeypatch.setattr). 
The count can only stay the same or decrease -- increasing it fails the test.

Ratchets are guidance and reminders about good code, not rules to be blindly obeyed. When a ratchet fires on your code:

1. Understand *why* the ratchet exists by reading its `rule_description`. It explains the principle behind the check.
2. Fix the code in the spirit of the ratchet. For example, if `PREVENT_MONKEYPATCH_SETATTR` fires, a valid fix could be to use dependency injection -- not to manually save/restore the attribute with `try/finally`, which evades the regex while violating the same principle.
3. Never evade a ratchet. Restructuring code to dodge the regex pattern while still doing the same bad thing is worse than the original violation, because it hides the problem. Common evasion patterns include splitting a statement across lines, assigning to a temporary variable before the flagged operation, or using a synonym that the regex doesn't catch.
4. If you cannot find a fix that honors the spirit of the ratchet, **flag this to the user** rather than silently working around it. Do not use type-system escape hatches (e.g. assigning through `Any`, intermediate variables, or synonyms) to bypass a ratchet -- these are evasions even if they dodge the regex.
5. If the ratchet is a **true misfire** -- the regex pattern matched something that is genuinely not the anti-pattern it was designed to catch (e.g. a variable name that happens to contain a flagged substring, or a string literal / comment that matches the pattern) -- then first try to update the ratchet's regex to be more specific so it no longer misfires (be extra careful not to exclude any real violations in the process). If that's not feasible, bump the ratchet count and explain the misfire to the user. This is distinct from a case where there *is* a real violation but you believe it's "justified"; justified violations are still violations and should be handled per steps 1-4 above.

## Test fixture discovery

Before writing new tests, read the relevant `conftest.py` and `testing.py` files to avoid reimplementing things that already exist. 
Test infrastructure lives in these files:

| File pattern | Purpose |
|---|---|
| `conftest.py` | Pytest fixtures and hooks, scoped to the directory they're in (auto-discovered by pytest) |
| `testing.py` | Non-fixture test utilities: factory functions, helpers, context managers (explicitly imported) |
| `mock_*_test.py` | Concrete mock implementations of interfaces (explicitly imported) |

All fixtures must be in conftest.py, not in individual test files.

# Manual verification and testing

Before declaring any feature complete, manually verify it: exercise the feature exactly as a real user would, with real inputs, and critically evaluate whether it *actually does the right thing*. 
Do not confuse "no errors" with "correct behavior" -- a command that exits 0 but produces wrong output is not working.

Then crystallize the verified behavior into formal tests. 
Assert on things that are true if and only if the feature worked correctly -- this ensures tests are both reliable and meaningful.

## Verifying interactive components with tmux

For interactive components (TUIs, interactive prompts, etc.), use `tmux send-keys` and `tmux capture-pane` to manually verify them. 
This is a special case: do NOT crystallize these into pytest tests. 
They are inherently flaky due to timing and useless in CI, but valuable for agents to verify that interactive behavior looks right during development.

# Communication

If the user talks to you about files or directories on disk, assume (unless context indicates otherwise) they mean their local disk, not the one in your sandbox -- use the `file-sharing` skill to bridge the two.

# Work delegation

You can delegate larger tasks to sub-agents using the `launch-task` skill.
Sub-agents work on separate git branches and are labeled with `workspace=$MINDS_WORKSPACE_NAME` so you can track them.

Use your judgment on when to do work directly vs delegating. Delegation is useful for:
- Tasks large enough to warrant a separate context
- Multi-file changes that benefit from verification before merging
- Long-running operations you don't want to block on

# Finding past work

Chats from agents that have run on this host -- current or past, including ones
that were destroyed -- are stored locally on this host and are recoverable, so
never tell the user you can't access an earlier or deleted conversation without
checking first. Use the `find-past-transcripts` skill to find and read them.

# Self-modification

You can (and should) modify your own configuration to improve yourself:

- **CLAUDE.md**: (this file) update these instructions if you discover better ways to operate.
- **.agents/skills/**: Create new skills or modify existing ones. Each skill is a directory with a SKILL.md file. (Also symlinked from `.claude/skills/`.)
- **supervisord.conf**: Add, modify, or remove background services. See the `update-service` skill.
- **scripts/**: Add utility scripts that help you accomplish your purpose.

Commit your changes to git after making modifications.

Inspirations are a publishable, reusable, bootable snapshot of the apps and features a mind has built (one repo can accumulate several); another mind can adapt one into itself.

# Updates

Use the `update-self` skill to pull improvements from the upstream template repo, and the `submit-upstream-changes` skill to push shared changes (skills, scripts, config) back upstream.
The upstream is defined in `parent.toml`.

# Using crystallized skills

- **Prefer an applicable skill over reinventing.** Skill descriptions are auto-injected into your context, so match by purpose, not by name.

- **Run a skill's steps one at a time in chat.** When a skill exposes per-step subcommands (plus a `run all`), drive the subcommands individually -- mirror each as a `tk` step and surface its output -- so the user gets a rich progress view, pausing only at the skill's declared `[prose]` steps. Reserve `run all` for headless or scheduled runs where there's no chat to show progress in.

- **Live first, ratify at turn-end.** Handle the user's immediate request *live* in the current chat to keep it interactive; at turn-end, formalize the work through the relevant lifecycle skill, which runs its hardening pass in a background worker (never inline in the main agent). Route by situation:
  - Net-new task needing research or experimentation -> `do-something-new` (it routes to `fetch-process-show` for data or `build-web-service` for a web view).
  - Just-finished work that's cohesive, likely to recur, and mostly deterministic -> `crystallize-artifact` to promote it into a committed, tested skill.
  - A skill errored or gave a wrong result -> work around it live, then `heal-artifact` at turn-end. Never patch the skill inline.
  - You changed an existing skill, or a skill ran but needed manual post-processing -> `update-artifact` at turn-end so the change is verified and the skill swallows the gap.

  For non-skill contract-bearing files (hook scripts, this file) there is no worker pipeline -- apply the live change carefully and add manual rigor at turn-end (real fixtures, end-to-end exercise of new code paths).

# Memory

Use Claude's built-in memory system. Your memory directory is `runtime/memory/` (configured via `autoMemoryDirectory` in `.claude/settings.json`).
Memory is gitignored from the main branch. When the user has enabled GitHub sync (the `github-sync` skill), the github-sync service ships it -- with the rest of `runtime/` -- to the `runtime-sync` branch of the workspace's private sync repo, so it survives container loss.

# Services

**Before editing any code that belongs to a supervisord service -- a user-facing web service or a background daemon -- load the `update-service` skill first.** It owns the live change loop (apply, refresh, verify) and the turn-end hardening flow; do not hand-edit a service's code or `supervisord.conf` without it.

You can define background services as supervisord programs in `supervisord.conf`.
Supervisord (launched by `bootstrap` after first-boot setup) supervises them; each program writes its own rotated logs under `/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
To add, change, or remove a service, edit `supervisord.conf` and run `supervisorctl reread && supervisorctl update` (and `supervisorctl restart <name>` to bounce one). Inspect with `supervisorctl status` / `supervisorctl tail -f <name> stderr`.
See the `update-service` skill for details.

For routine jobs that run on a cadence and then exit (backups, health checks, the weekly Caretaker -- off by default, see the enable-caretaker skill), use cron via the **`manage-scheduled-tasks`** skill rather than a supervisord program; and after building or editing any service, use the `check-app-errors` skill to scan `/var/log/supervisor/` for errors (a clean exit code does not mean the service is healthy).

# Git

Commit your changes locally.
`runtime/` is gitignored from the main branch (it includes `runtime/memory/` for Claude memory and other transient state).

Chat file uploads (files a user attaches to a message) are stored in the top-level `uploads/` directory inside the repo working tree -- NOT under `runtime/`. Uploads can be arbitrarily large and any format, so they don't belong in version-controllable content; `uploads/` is gitignored. Being outside `runtime/`, uploads are NOT carried by the opt-in GitHub sync (which ships only `runtime/`), but the host-level `host-backup` service (a restic snapshot of the whole host dir) does capture them, so uploads still survive container loss. See `libs/host_backup/README.md`.

GitHub sync is opt-in via the `github-sync` skill. When the user has enabled it: a `post-commit` hook auto-pushes the active branch of every checkout to `origin` (the workspace's dedicated private repo) in the background -- you do not need to push manually; the hook never blocks the commit, and output is captured at `/tmp/post-commit-push.log`. The `github-sync` service additionally syncs `runtime/` onto a separate orphan branch (`runtime-sync`) on the same `origin`. See `libs/github_sync/README.md`.

When GitHub sync is not enabled, there is no auto-push and no GitHub remote to push to; commits stay local (the restic `host-backup` still protects the whole host dir).

- Don't include auto-generated lockfile churn (`uv.lock`, `package-lock.json`, etc.) in commits unless the change intentionally bumps a dependency.

# Silly error workarounds

If you get a failure in `test_no_type_errors` that seems spurious, try running `uv sync --all-packages` and then re-running the tests. If that doesn't work, the error is probably real, and should be fixed.

If you get a "ModuleNotFoundError" error for a 3rd-party dependency when running a command that is defined in this repo (like `mngr`), then run "uv tool uninstall imbue-mngr && uv tool install -e vendor/mngr" (for the relevant tool) to refresh the dependencies for that tool, and then try running the command again.

If you get a failure when trying to commit the first time, just try committing again (the pre-commit hook returns a non-zero exit code when ruff reformats files).

# Dealing with the unexpected

If something unexpected happens -- errors, confusing state, things not working as documented -- use the `dealing-with-the-unexpected` skill for guidance.

A background OOM-prevention daemon (earlyoom) kills ("sheds") memory-heavy processes under sustained memory pressure -- most-expendable first (an agent's build/test/browser subprocesses before the agent itself). If a command of yours dies with exit 137 (or SIGKILL/SIGTERM) and you did not kill it, confirm by checking the shed ledger at `/mngr/code/runtime/oom_priority/events/shed.jsonl` for a record naming it (matched by pid or process name). If it was shed, do NOT blindly re-run a memory-heavy command -- it will likely be shed again; find a lower-memory approach (smaller batches, streaming, releasing data you no longer need) and only retry if you can.
