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
- This project uses a CLI ticket system (`tk`) for task management. Run `tk help` when you need to use it. Tickets live under `runtime/tickets/` (the path is set via the `TICKETS_DIR` env var so tickets ride the `mindsbackup/$MNGR_AGENT_ID` runtime-backup branch).
- All relative paths in this repo assume cwd = repo root (`/code`). Supervisord runs the services from there; any process started elsewhere (manual launch, subprocess from a different cwd) must either set cwd to the repo root or use absolute paths. State directories live under `runtime/<feature>/`.
- When adding a new web app, do NOT edit `libs/web_server/` -- it's an example placeholder. Use the `build-web-service` skill, which sets up a new lib + service entry + `forward_port.py` registration on its own port.

# Task management (CRITICAL — read this before doing real work)

You manage your work using `tk`, the vendored ticket tracker at `vendor/tk/`. This is the **only** task tracking tool available to you. Claude Code's built-in `TodoWrite` is disabled — attempts to call it will be denied. **`tk create --step` is the replacement for `TodoWrite`**: use it everywhere you would have used `TodoWrite` to declare plan steps and track completion. The difference is that `tk` step records render directly in the user-facing chat progress view rather than being hidden in a side panel.

## Two kinds of records: steps and tickets

`tk` stores two distinct kinds of records, distinguished by the `step:` frontmatter field:

- **Step records** (`tk create --step "..."`) are *turn-bound progress markers* — the direct replacement for Claude Code's `TodoWrite`. They populate the chat progress view: each step becomes a node on the timeline with a status icon and (on close) a one-line summary. Steps are **creator-private** — only the agent that created them sees them. They are sequential within a turn, ephemeral by intent, and exist purely to communicate progress to the user.
- **Regular tickets** (`tk create "..."`, no `--step`) are *substantive work units worth tracking cross-agent*. Other agents can see them, pick them up, and own them. A ticket is "current" to whichever agent has it `in_progress` and assigned to themselves. Regular tickets are a cross-agent backlog tool only; they do **not** render in the chat progress view (only step records do).

Most turns use only step records. Tickets enter the picture when a piece of work is large enough to span turns or to be handed between agents.

## Why this exists

Your conversation is rendered to the user as a "progress view": each turn shows a clean, vertical timeline of plain-English task steps with one-line summaries on completion. The user does **not** see your raw tool calls unless they explicitly expand a step.

This means every step title and every closing summary is **user-facing copy**. Write it for a non-technical reader who doesn't know your codebase, your tools, or your jargon.

## How every turn must start: declare the plan as steps

**The first thing you do on any user prompt that warrants real work is decompose it into a sequence of step records and create them all up front, BEFORE doing any of the work.** This is not optional. The sequence of steps is the user-visible plan; making the user wait while tool calls scroll by before they see the plan defeats the entire purpose of the progress view.

Concretely, the start of every substantive turn looks like:

1. (Optional) One short prose acknowledgement to the user — e.g. "Sure, looking into that now." This renders as plain text above the timeline. Keep it to one line; do not narrate the plan here.
2. `tk create --step "..."` for every step you currently expect the work to require, in order. Each `tk create --step` prints `Created <id>: <title>` — note each id (you pass it to `tk start`/`tk close`). Do NOT call `tk start` on any of them yet.
3. `tk start` the first step and begin the work for that step.

You may add steps later (`tk create --step` more as new sub-problems surface) or remove ones that turn out to be unneeded (`tk close <id> "No longer needed — the previous step covered this."`).

**Steps must be serial.** Only one step is `in_progress` at a time. Do not call `tk start` on the next step until the current one is `tk close`d.

A step represents a logical step in the user's mental model of the work, not a unit of parallel computation. If you'd describe the work to the user as "first I'll do X, then Y, then Z," that's three steps. If you'd describe it as a single coherent step — even if it involves several parallel lookups internally — it's one step.

Granularity: typically 2-5 sequential steps per substantive turn. Not one per tool call (way too granular). Not one step for the whole turn (defeats the purpose of progress).

## When you don't need any records

Records are required for any turn that involves real work. The exceptions:

- Chitchat, single-line acknowledgements, trivial answers ("yes, I can do that", "the file is at src/foo.ts").
- Pure clarifying-question turns where you have nothing concrete to do until the user answers.
- A reply that's a single quick read of one file to answer the user's question.

If your turn is truly one of these, just write your reply directly — no records, no timeline. If you're not sure, default to creating steps; an extra small step is better than the user watching a turn unfold with no visible plan.

## Step lifecycle (must follow exactly)

At the **start of the turn**, create every step you currently expect the work to require. Each `tk create --step` prints `Created <id>: <title>`; note the id it prints (you can batch the creates in one tool call):

```bash
tk create --step "Look through your recent changes to find the new theme"
tk create --step "Trace how the dark-mode toggle picks a theme"
tk create --step "Register the new theme and update the toggle"
tk create --step "Verify the toggle now reaches your new theme"
```

This prints, e.g.:

```
Created cod-step-a1b2: Look through your recent changes to find the new theme
Created cod-step-c3d4: Trace how the dark-mode toggle picks a theme
...
```

Title rules: plain English, describes the goal as the user understands it, no file names, no tool names, no internal jargon. The title is what the user sees as the step name.

Then for **each step in order**, one at a time, using the literal id from the `Created` line (run `tk steps` if you need to re-list the ids):

1. **Start** it:
   ```bash
   tk start cod-step-a1b2
   ```
2. **Do the work.** Run whatever tools you need.
3. **Close with a summary** (required, positional):
   ```bash
   tk close cod-step-a1b2 "Read through your recent commits and the theme files to find what's new."
   ```
4. Move on to the next step. Only one is `in_progress` at a time.

**Run `tk start` and `tk close` each as the only command in their tool call** — no `cd` prefix, no chaining (`&&`, `;`, `|`, `&`, a newline), no output redirection; otherwise the progress view can't place the step. (This applies only to `start`/`close` — you can still batch `tk create --step` calls when declaring the plan.)

If during the work you discover a sub-problem that warrants its own step, `tk create --step` a new one (it'll appear at the bottom of the timeline). If you discover a previously-planned step is no longer needed, `tk close <id> "No longer needed — covered by the previous step."` (as its own command).

Summary rules: ONE concise line, plain English, describing **the work you did in this step** — what the user would see if they expanded the block. Think of it as a high-level non-technical caption for the raw tool calls inside.

- It is NOT the *result* / *finding* / *answer* — those go in your final assistant message below the timeline. Don't put conclusions, fixes, or recommendations in summaries.
- It is NOT a list of tool calls or file names. "Ran git log, then read midnight.ts, then grep'd for registerTheme" is wrong — too technical, and the user can already see those tool calls if they expand the block.
- It IS one short caption-style sentence describing the work, in the same plain-English tone as the title.

After all your steps for the turn are closed, write your final user-facing assistant message — *this* is where the actual results, findings, and recommendations belong. It renders below the progress timeline as the agent's reply to the user.

**Steps and prose:**
- Text emitted while a step is `in_progress` shows as a live caption under the step, replaced by each new message.
- **Close your final step *before* writing your user-facing wrap-up reply.** The progress view promotes the final run of prose with no step open to your top-level reply below the timeline; prose you speak *inside* a step, after its last work, is ejected to the inline stream just after that step. So closing the last step first keeps your wrap-up cleanly below the timeline. This is best-effort, not a hard rule — the view renders sensibly either way.
- **Never mention steps, tickets, or `tk` in what you say to the user.** The step machinery is invisible to them — don't narrate it ("closing this step," "moving on to the next step," "starting X"). Speak only about the actual work and its subject matter, as if the timeline weren't there. Step titles and close summaries are the one place step structure may surface, and even those describe the *work*, not the act of tracking it.
- Steps may stay open across turns. Close when work is done; leave open if work continues.

## Working with regular tickets

Regular tickets are for substantive work worth tracking cross-agent. The relevant commands:

- `tk ls` / `tk ready` / `tk blocked` — list regular tickets (step records are hidden by default; pass `--include-steps` or `--only-steps` to override).
- `tk show <id>` — show a single ticket with its child sub-tickets and `## Steps` sub-records.
- `tk create "..."` — file a new ticket. Inside an mngr context the ticket is stamped with `agent: $MNGR_AGENT_NAME` (creator) but left **unassigned** until someone picks it up.
- `tk start <id>` — pick up a ticket. Auto-self-assigns you (`assignee: $MNGR_AGENT_NAME`). If the ticket was already assigned to a different agent, you get a stderr warning and the reassign proceeds. From this point on the ticket shows in *your* progress view, not the originator's.
- `tk close <id> [summary]` — close. Summary is optional for tickets (required for steps).
- `tk assign <id> [agent]` / `tk unassign <id>` — explicit assignment.

Tickets can be left `in_progress` across turns until you close them. (Regular tickets are not shown in the chat progress view — that view renders step records only. Use `tk ls` / `tk show` to track tickets.)

## Persistent task state across turns

Step records persist across user turns until you close them. At the start of every new user message, you'll receive a system reminder listing every **step record** still `open` or `in_progress` for you. For each one, decide before doing anything else:

- **Keep working on it** — appropriate if the new user message asks you to continue. Run `tk start <id>` if it isn't already in_progress, then proceed.
- **Replace it** — appropriate if the new user message redirects you. Close the old step with a summary of what state you left things in, then create new steps for the new direction.
- **Close it** — appropriate if you didn't get to it but you're moving on. Close it with a summary that honestly reports the situation, e.g. `tk close <id> "Did not get to this — got pulled into the dark-mode bug instead."`. Do not silently abandon it.

The reminder hook intentionally only shows **steps**, not tickets. Tickets are managed cross-agent through `tk ls / tk ready / tk show` — you invoke those yourself when you want to check your queue.

## No "failed" status

Every record terminates as `closed`, regardless of outcome. There is no "failed" or "abandoned" state in this system. If a step didn't pan out:

- Still close it. The summary describes the *work you did* (e.g. "Tried to reproduce the bug by running the export endpoint with several sample inputs."), and your final assistant message reports the *result* honestly.
- Don't leave a step open hoping to come back. Close it; if the user wants to continue, the next turn can open a fresh step.

## Subagent delegation

When you delegate to a sub-agent via the `launch-task` skill, the entire delegation is **one step** in your progress, not many. The sub-agent runs in its own container with its own `.tickets/` and uses tk independently for its own internal progress — that work does not surface in your chat. You represent the delegation in your progress with a single step like "Delegate the auth refactor to a sub-agent and review the result", then close it with a summary of the outcome when the sub-agent finishes.

## Read tk's help if you forget the commands

```bash
tk help
```

The everyday lifecycle you need is `tk create --step`, `tk start`, `tk close <id> "summary"`. For ticket pickup it's `tk start <id>` (auto-assigns) and `tk close <id>` (summary optional). Avoid `deps`, `links`, `types`, `priorities` — they exist for backlog management but are not used by the chat progress view.

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
- For browser automation, Playwright's Python API is available in the root venv -- use `from playwright.sync_api import sync_playwright` in a script invoked via `uv run python`. The Chromium browser itself (and its apt system libraries) installs asynchronously on first container boot via the one-shot `deferred-install` supervisord program rather than being baked into the image; if the install hasn't finished yet, any `playwright.chromium.launch()` call will fail with a clear error. Check the marker file `/var/lib/minds/deferred-install/done.playwright` (or run `supervisorctl status deferred-install`, or read `/var/log/supervisor/deferred-install-stdout.log`) to confirm the install completed before using browser automation in a fresh workspace. See `libs/bootstrap/README.md` for the full deferral contract. Chromium works as-is under the docker provider's gVisor (runsc) runtime -- gVisor allows user namespaces, so Chromium's namespace sandbox starts even though the container runs as root (verified: `chromium.launch()` succeeds with and without `--no-sandbox`). If you ever hit a "No usable sandbox!" error (e.g. on a host/runtime that doesn't permit unprivileged user namespaces), pass `chromium.launch(args=["--no-sandbox"])` (or `chromium_sandbox=False`) -- gVisor is the security boundary, so disabling Chromium's in-browser sandbox there is acceptable.

# Always remember these guidelines:

- When the user is actively interacting with you, prioritize delivering a result they care about over technical polish. Technical refinement can happen in the background.
- Never misrepresent your progress. It is far better to say "I made some progress but didn't finish" than to say "I finished" when you did not.
- Always finish your response by reflecting on your work and identify any potential issues.
- If I ask for something that seems misguided, flag that immediately. Then attempt to do whatever makes the most sense given the request, and in your final reflection, be sure to flag that you had to diverge from the request and explain why.
- During your final reflection, if you see a potentially better way to do something (e.g. by using an existing library or reusing existing code), flag that as a potential task for future improvement.
- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- Be concise in your communications. Don't hype up your results, say "perfect!", or use emojis. Be serious and professional.
- **Feedback systems combine binary and free-form signals.** When building anything that learns from user feedback, include *both* a basic binary signal (thumbs up/down, keep/skip, or whatever fits) *and* free-form text routed through an LLM judge -- unless the user specifies a different mechanism, which overrides this default. Binary is low-friction; free-form captures nuance you can't anticipate. The exact form depends on what's being built, but both should be present and intuitively accessible. Don't prescribe rigid taxonomies beyond the binary signal upfront.
- **Default UI is web view.** When exposing a tool to the user, default to a web page. Don't enumerate options (CLI / telegram / status line / web) -- just propose the web view and only deviate when there's a specific reason (CLI for batch jobs, telegram for push-only notifications, etc.).
- **Always preserve and surface the raw data and its source.** Anything you build *on top of* data -- a view, a summary, a derived metric -- sits between the user and the underlying records, so two things are non-negotiable. (1) *Preserve*: persist the raw source records the thing was built from, plus a reference to where they live (a URL, an API id, whatever lets you or the user get back to the origin) -- durably, not just in memory for the current run. Don't fetch-transform-discard. (2) *Surface*: give the user a clean, unprompted way to view that raw record or jump to its source -- they should never have to ask for it. "Raw data" means the source records the view was built from plus a link to where they live; for fuzzier cases (a metric computed from many calls) use judgement, but err toward keeping more. Preservation means a later change in processing requirements needs no refetch; surfacing means the user can bridge any gap the derived view leaves (a missing field, a rendering the agent didn't anticipate) without waiting on you. **Render the raw record in its native format** -- an HTML email shown as the rendered email, JSON pretty-printed, markdown rendered -- not dumped as escaped source text. "Raw" means *unprocessed by your derivation*, not *unrendered*: the goal is the faithful original as a human would view it, minus your summarization. **Keep all of this subtle.** Build the preserve and surface affordances in by default, but don't announce in chat that you're saving the data or adding a "view raw" control -- it should simply be there when the user wants it, not narrated as a feature.
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

To talk to the user, always go through the `send-user-message` skill. It
probes for configured channels (telegram, etc.) and dispatches; if none is
configured, it falls back to writing the message inline in your current
response. Do NOT hardcode a specific channel from other skills.

If the deployment happens to use telegram, incoming messages arrive via
`mngr message` from the telegram bot running in a background tmux window.
`send-user-message` handles that case; `send-telegram-message` and
`read-telegram-history` are the telegram-specific implementation details
it delegates to.

If the user talks to you about files or directories on disk,
unless context indicates otherwise, assume they mean their local
disk, not the one in your sandbox. (Use the file-sharing skill to
bridge the two if needed.)

# Work delegation

You can delegate larger tasks to sub-agents using the `launch-task` skill.
Sub-agents work on separate git branches and are labeled with `workspace=$MINDS_WORKSPACE_NAME` so you can track them.

Use your judgment on when to do work directly vs delegating. Delegation is useful for:
- Tasks large enough to warrant a separate context
- Multi-file changes that benefit from verification before merging
- Long-running operations you don't want to block on

# Self-modification

You can (and should) modify your own configuration to improve yourself:

- **CLAUDE.md**: (this file) update these instructions if you discover better ways to operate.
- **.agents/skills/**: Create new skills or modify existing ones. Each skill is a directory with a SKILL.md file. (Also symlinked from `.claude/skills/`.)
- **supervisord.conf**: Add, modify, or remove background services. See the `edit-services` skill.
- **scripts/**: Add utility scripts that help you accomplish your purpose.

Commit your changes to git after making modifications.

# Updates

Use the `update-self` skill to pull improvements from the upstream template repo, and the `submit-upstream-changes` skill to push shared changes (skills, scripts, config) back upstream.
The upstream is defined in `parent.toml`.

# Using crystallized skills

- **Prefer an applicable skill over reinventing.** Skill descriptions are
  injected so you can match by purpose, not by name.

- **Run a skill's steps step by step in chat.** When you invoke a skill in a
  chat turn and it exposes per-step subcommands (plus a `run all`), drive the
  subcommands one at a time -- mirror each as a `tk` step and surface its
  output -- so the user gets a rich progress view, rather than running one
  opaque `run all`. This is not a watch-and-steer mode: run straight through,
  pausing for the user only at the skill's declared `[prose]` steps. Reserve
  `run all` for headless or scheduled runs, where there is no chat to show
  progress in.

- **Live first, ratify at turn-end via the worker pipeline.** The
  lifecycle skills follow the same shape: handle the user's immediate
  request *live* in the current chat to keep it interactive and iterative;
  at turn-end, formalize the work through a background worker. Three shared
  references carry the core: the **live** half is the interactive-delivery
  shape (`.agents/shared/references/interactive-delivery.md`), specialized
  by `do-something-new`'s routes (`fetch-process-show` for data,
  `build-web-service` for web views); the turn-end **harden pass** follows
  the universal contract
  (`.agents/shared/worker/references/harden-artifact.md`), driven by three generic
  operation leads -- `crystallize-artifact` (create), `update-artifact`
  (change), `heal-artifact` (fix) -- each parameterized by the artifact
  (skill / service / system-interface). A single generic `harden-worker`
  composes that contract with one operation reference (`op-crystallize.md` /
  `op-update.md` / `op-heal.md`) and one artifact reference
  (`artifact-skill.md` / `artifact-service.md` / `artifact-system-interface.md`)
  per task. The leads sit on the worker **plumbing**, `lead-proxy.md` +
  `worker-reporting.md`. The harden pass **always runs in a background
  worker** -- the main agent never runs the code-guardian gates or the
  thorough test passes itself (do not start those flows in the main agent).
  If you find yourself committing a change to any contract-bearing file (a
  skill, a hook script with a documented contract, an invariant elsewhere)
  and stopping there, you've skipped the ratify step. The live phase is
  necessary but not sufficient -- the worker pipeline exists to add the rigor
  that's awkward to do interactively.

  Concrete cases:
  - **Net-new task needing research / experimentation**: invoke
    `do-something-new`. It routes to `fetch-process-show` (data) or
    `build-web-service` (web view) -- or applies the shared
    interactive-delivery shape directly when neither fits -- to drive the
    live phase. Both hand their confirmed artifact to `crystallize-artifact`
    (artifact = skill for the data pipeline, service for the web view).
  - **Crystallization nudge** after a normal turn that turned out to be
    cohesive, likely to recur, and mostly deterministic: invoke
    `crystallize-artifact` (artifact = skill) to ratify the just-finished
    work. Otherwise acknowledge and move on. (This is a manual judgement at
    turn-end, not a wired hook.)
  - **A skill errored or delivered a wrong result**: fulfil the user's
    request live by working around the failure, then at turn-end invoke
    `heal-artifact`. Never patch the skill inline -- `heal-artifact` is the
    ratify path.
  - **You and the user discussed and applied a change to an existing
    skill**: edit live so the user can iterate, then at turn-end invoke
    `update-artifact` (committed origin -- skips the design gate, verifies
    the live commit). Direct Edit + commit skips the ratification. (For
    non-skill contract-bearing files like hook scripts or CLAUDE.md itself,
    no worker pipeline exists today -- apply the live phase carefully and add
    manual rigor at turn-end: real test fixtures, end-to-end exercise of new
    code paths, etc.)
  - **A skill use was successful but required manual post-processing**:
    do the post-work live, then at turn-end invoke `update-artifact`
    (emergent origin) so the skill swallows the gap.

# Memory

Use Claude's built-in memory system. Your memory directory is `runtime/memory/` (configured via `autoMemoryDirectory` in `.claude/settings.json`).
Memory is gitignored from the main branch but is backed up automatically by the runtime-backup service onto the `mindsbackup/$MNGR_AGENT_ID` branch when `GH_TOKEN` is set, so it survives container loss.

# Services

You can define background services as supervisord programs in `supervisord.conf`.
Supervisord (launched by `bootstrap` after first-boot setup) supervises them; each program writes its own rotated logs under `/var/log/supervisor/<name>-stdout.log` and `/var/log/supervisor/<name>-stderr.log`.
To add, change, or remove a service, edit `supervisord.conf` and run `supervisorctl reread && supervisorctl update` (and `supervisorctl restart <name>` to bounce one). Inspect with `supervisorctl status` / `supervisorctl tail -f <name> stderr`.
See the `edit-services` skill for details.

# Git

Commit your changes locally.
`runtime/` is gitignored from the main branch (it includes `runtime/memory/` for Claude memory and other transient state).

A `post-commit` hook installed via `core.hooksPath = /mngr/code/scripts/git_hooks` auto-pushes the active branch to `origin` in the background, but only when `GH_TOKEN` is set in the environment. You do not need to push manually. The hook never blocks the commit; output is captured at `/tmp/post-commit-push.log`.

`runtime/` is backed up automatically by the `runtime-backup` service onto a separate orphan branch (`mindsbackup/$MNGR_AGENT_ID`) on the same `origin`, also gated on `GH_TOKEN`. See `libs/runtime_backup/README.md`.

If `GH_TOKEN` is unset, both auto-pushes silently no-op; commits stay local.

- Don't include auto-generated lockfile churn (`uv.lock`, `package-lock.json`, etc.) in commits unless the change intentionally bumps a dependency.

# Silly error workarounds

If you get a failure in `test_no_type_errors` that seems spurious, try running `uv sync --all-packages` and then re-running the tests. If that doesn't work, the error is probably real, and should be fixed.

If you get a "ModuleNotFoundError" error for a 3rd-party dependency when running a command that is defined in this repo (like `mngr`), then run "uv tool uninstall imbue-mngr && uv tool install -e vendor/mngr" (for the relevant tool) to refresh the dependencies for that tool, and then try running the command again.

If you get a failure when trying to commit the first time, just try committing again (the pre-commit hook returns a non-zero exit code when ruff reformats files).

# Dealing with the unexpected

If something unexpected happens -- errors, confusing state, things not working as documented -- use the `dealing-with-the-unexpected` skill for guidance.

# claude -p

If ever building AI-powered services and wanting to use `claude -p`, make sure to unset the MAIN_CLAUDE_SESSION_ID for the process. This prevents conversation rendering issues.