# Critical context

IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS IN THIS FILE DURING YOUR WORK ON THIS PROJECT.

IF YOU FAIL TO FOLLOW ONE, YOU MUST EXPLICITLY CALL THAT OUT IN YOUR RESPONSE.

# Important things to know:

- You are running in a tmux session inside a container or sandbox that was created via `mngr`
- This is a monorepo.
- Run commands by calling "uv run" from the root of the git checkout (ex: "uv run mngr create ...").
- NEVER amend commits or rebase--always create new commits.
- If you ever need to work with another *git* repo that is *outside* of this monorepo, you should do so by adding a git subtree under vendor/

# Task management (CRITICAL — read this before doing real work)

You manage your work using `tk`, the vendored ticket tracker at `vendor/tk/`. This is the **only** task tracking tool available to you. Claude Code's built-in `TodoWrite` is disabled — attempts to call it will be denied. There is no fallback, no alternative tool, and no exception.

## Why this exists

Your conversation is rendered to the user as a "progress view": each turn shows a clean, vertical timeline of plain-English task steps with one-line summaries on completion. The user does **not** see your raw tool calls unless they explicitly expand a step. Your `tk` tickets are the data source for this view — every ticket you create becomes a step in the timeline; every summary you write on close becomes the result text under that step.

This means every ticket title and every closing summary is **user-facing copy**. Write it for a non-technical reader who doesn't know your codebase, your tools, or your jargon.

## When to use a ticket

- Use a ticket whenever you do meaningful work in a turn — anything more than a quick reply or a single read of one file to answer a question.
- Don't use a ticket for chitchat, single-line acknowledgements, or trivial answers ("yes, I can do that"). Turns with no tickets render as plain conversation.
- A good rule of thumb: if a non-technical user reading "Step: <your title>" would think "yes, that's what I asked for", you have a ticket. If they'd think "what does that even mean?", you don't.

## How to decompose work into tickets

**One ticket per sequential step. Tickets must be serial — do not start a new one until the previous one is closed.** If a chunk of work consists of operations you'd run in parallel or simultaneously, that whole chunk is *one* ticket, not many. Multiple tool calls inside one ticket is normal and expected.

A ticket represents a logical step in the user's mental model of the work, not a unit of parallel computation. If you'd describe the work to the user as "first I'll do X, then Y, then Z," that's three tickets. If you'd describe it as a single coherent step — even if it involves several parallel lookups internally — it's one ticket.

Granularity: typically 2-5 sequential tickets per substantive turn. Not one per tool call (way too granular). Not one ticket for the whole turn (defeats the purpose of progress).

## A short reply before tickets is fine

It's OK — encouraged, even — to write a brief one-line acknowledgement before you start creating tickets. e.g. "Sure, looking into that now." or "OK, let me dig in." That text appears as plain assistant prose above the progress timeline. Just keep it short; the timeline is where the actual progress lives.

## Lifecycle (must follow exactly)

For every step you commit to:

1. **Create** the ticket and capture the id:
   ```bash
   ID=$(tk create "Look through your recent changes to find the new theme")
   ```
   Title rules: plain English, describes the goal as the user understands it, no file names, no tool names, no internal jargon. Title is what the user sees as the step name.

2. **Start** it before you begin work:
   ```bash
   tk start "$ID"
   ```

3. **Do the work.** Run whatever tools you need.

4. **Write a summary as a note**, then close:
   ```bash
   tk add-note "$ID" "Read through your recent commits and the theme files to find what's new."
   tk close "$ID"
   ```
   Summary rules: ONE concise line, plain English, describing **the work you did in this step** — what the user would see if they expanded the block. Think of it as a high-level non-technical caption for the raw tool calls inside.

   - It is NOT the *result* / *finding* / *answer* — those go in your final assistant message below the timeline (e.g. "Fixed: the new theme wasn't registered with the toggle. Made it available."). Don't put conclusions, fixes, or recommendations in summaries.
   - It is NOT a list of tool calls or file names. "Ran git log, then read midnight.ts, then grep'd for registerTheme" is wrong — too technical, and the user can already see those tool calls if they expand the block.
   - It IS one short caption-style sentence describing the work, in the same plain-English tone as the title. "Read through your recent commits and the theme files to find what's new." "Edited the theme switcher so it cycles through every registered theme." "Tested the export in a fresh browser to confirm it now opens in Excel."

After all your tickets for the turn are closed, write your final user-facing assistant message — *this* is where the actual results, findings, and recommendations belong. It renders below the progress timeline as the agent's reply to the user.

## Persistent task state across turns

Tickets persist across user turns until you close them. At the start of every new user message, you'll receive a system reminder listing every ticket that is still `open` or `in_progress`. For each one, decide before doing anything else:

- **Keep working on it** — appropriate if the new user message asks you to continue. Run `tk start <id>` if it isn't already in_progress, then proceed.
- **Replace it** — appropriate if the new user message redirects you. Close the old ticket with a summary of what state you left things in, then create new tickets for the new direction.
- **Close it** — appropriate if you didn't get to it but you're moving on. Close it with a summary that honestly reports the situation, e.g. "Did not get to this — got pulled into the dark-mode bug instead." Do not silently abandon it.

If a ticket appears in the reminder but you didn't start it, just close it (with a brief summary explaining why) or leave it as-is and start it now.

## No "failed" status

Every ticket terminates as `closed`, regardless of outcome. There is no "failed" or "abandoned" state in this system. If a step didn't pan out:

- Still close the ticket. The summary describes the *work you did* (e.g. "Tried to reproduce the bug by running the export endpoint with several sample inputs."), and your final assistant message reports the *result* honestly (e.g. "I couldn't reproduce — the endpoint returns a valid file in my testing. Could you share a sample input that produces the error?").
- Don't leave a ticket open hoping to come back. Close it; if the user wants to continue, the next turn can open a fresh ticket.

The combination of an honest work-description summary plus an honest result in the final message is far more useful than a ticket that lingers open across many turns.

## Subagent delegation

When you delegate to a sub-agent via the `launch-task` skill, the entire delegation is **one ticket** in your progress, not many. The sub-agent runs in its own container with its own `.tickets/` and uses tk independently for its own internal progress — that work does not surface in your chat. You represent the delegation in your progress with a single ticket like "Delegate the auth refactor to a sub-agent and review the result", then close it with a summary of the outcome when the sub-agent finishes.

## Read tk's help if you forget the commands

```bash
tk help
```

The full lifecycle you need is just `create`, `start`, `add-note`, `close`. Avoid the others (deps, links, types, priorities) — they exist for backlog management but are not used by the chat progress view.

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
- For browser automation, Playwright's Python API is available in the root venv with Chromium preinstalled -- use `from playwright.sync_api import sync_playwright` in a script invoked via `uv run python`.

# Always remember these guidelines:

- Never misrepresent your progress. It is far better to say "I made some progress but didn't finish" than to say "I finished" when you did not.
- Always finish your response by reflecting on your work and identify any potential issues.
- If I ask for something that seems misguided, flag that immediately. Then attempt to do whatever makes the most sense given the request, and in your final reflection, be sure to flag that you had to diverge from the request and explain why.
- During your final reflection, if you see a potentially better way to do something (e.g. by using an existing library or reusing existing code), flag that as a potential task for future improvement.
- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- Be concise in your communications. Don't hype up your results, say "perfect!", or use emojis. Be serious and professional.

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
- Note that "uv run pytest" defaults to running all "unit" and "integration" tests, but the "acceptance" tests also run in CI. Do *not* run *all* the acceptance tests locally to validate changes--just allow CI to run them automatically after you finish responding (it's faster than running them locally).
- If you need to run a specific acceptance or release test to write or fix it, iterate on that specific test locally by calling "just test <full_path>::<test_name>" from the root of the git checkout. Do this rather than re-running all tests in CI.
- Note that tasks are *not* allowed to finish without A) all tests passing in CI, B) running /autofix to verify and fix code issues, and C) running /verify-conversation to review the conversation for behavioral issues.
- A PR will be made automatically for you when you finish your reply--do NOT create one yourself.
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

You communicate with the user via Telegram.
Incoming messages arrive automatically via `mngr message` from the telegram bot running in a background tmux window.

To send a message to the user, use the `send-telegram-message` skill.
To understand the conversation context before replying, use the `read-telegram-history` skill.

# Work delegation

You can delegate larger tasks to sub-agents using the `launch-task` skill.
Sub-agents work on separate git branches and are labeled with `workspace=$MINDS_WORKSPACE_NAME` so you can track them.

Use your judgment on when to do work directly vs delegating. Delegation is useful for:
- Tasks large enough to warrant a separate context
- Multi-file changes that benefit from verification before merging
- Long-running operations you don't want to block on

# Responding to events

You can create a persistent background watcher using the `create-event-processor` skill if you would like to automatically respond to certain events (e.g. new messages, tickets, or specific times of day).

# Self-modification

You can (and should) modify your own configuration to improve yourself:

- **CLAUDE.md**: (this file) update these instructions if you discover better ways to operate.
- **.agents/skills/**: Create new skills or modify existing ones. Each skill is a directory with a SKILL.md file. (Also symlinked from `.claude/skills/`.)
- **services.toml**: Add, modify, or remove background services. See the `edit-services` skill.
- **scripts/**: Add utility scripts that help you accomplish your purpose.

Commit your changes to git after making modifications.

# Updates

Use the `update-self` skill to pull improvements from the upstream template repo, and the `submit-upstream-changes` skill to push shared changes (skills, scripts, config) back upstream.
The upstream is defined in `parent.toml`.

# Memory

Use Claude's built-in memory system. Your memory directory is `memory/` (configured via autoMemoryDirectory).
Memory is gitignored -- it persists on the filesystem but is not version controlled.

# Services

You can define background services in `services.toml`. 
The bootstrap service manager (running in a separate tmux window) watches this file and starts/stops tmux windows accordingly.
See the `edit-services` skill for details.

# Git

Commit your changes locally. 
`runtime/` and `memory/` are gitignored.
Do not push to remote.

# Silly error workarounds

If you get a failure in `test_no_type_errors` that seems spurious, try running `uv sync --all-packages` and then re-running the tests. If that doesn't work, the error is probably real, and should be fixed.

If you get a "ModuleNotFoundError" error for a 3rd-party dependency when running a command that is defined in this repo (like `mngr`), then run "uv tool uninstall imbue-mngr && uv tool install -e vendor/mngr" (for the relevant tool) to refresh the dependencies for that tool, and then try running the command again.

If you get a failure when trying to commit the first time, just try committing again (the pre-commit hook returns a non-zero exit code when ruff reformats files).

# Dealing with the unexpected

If something unexpected happens -- errors, confusing state, things not working as documented -- use the `dealing-with-the-unexpected` skill for guidance.
