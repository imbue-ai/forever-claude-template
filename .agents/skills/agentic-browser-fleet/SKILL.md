---
name: agentic-browser-fleet
description: Drive a fleet of shared Chromium browsers yourself, one command at a time, from your shell. Use when the user wants you to do something on the web (log in somewhere, fill a form, click through a flow, read a page that needs interaction) rather than just fetch a URL. YOU hold the wheel: you run `agentic-browser-fleet` commands, look at the page, decide what to click, and click it -- in this same chat, with your own reasoning.
---

# Driving the browser fleet (you hold the wheel)

There are "two agents" here only in name. **You are the driver.** You run
`agentic-browser-fleet <cmd> <id> ...` commands directly. There is no
separate brain doing the thinking -- the thinking is *yours*, right here in
this chat.

The core loop is dead simple:

1. `state <id>` prints the page as a numbered list of clickable elements.
2. *You* read it and reason about which element you want.
3. You `click <id> <index>` (or `input`, `select`, `scroll`, ...).
4. You run `state <id>` again to see what changed, and repeat.

That's it. You are not handing off a goal and watching a trace scroll by;
you are looking at the page and operating it yourself, step by step, the way
a person clicking through a website would.

> There *is* an optional `task <id> "<goal>"` that hands a whole goal to an
> autonomous browser-use agent (it uses an LLM and needs an API key). That is
> a **fallback**, not the main path -- see the very end. Drive it yourself first.

Run every command from the repo root via `uv run`:

```bash
uv run agentic-browser-fleet <command> ...
```

It needs `MNGR_AGENT_ID` in the environment (set automatically inside an
agent shell). Without it the CLI exits `64`. If it can't reach the browser
daemon it exits `69`.

## The loop, worked end to end

```text
uv run agentic-browser-fleet open 0 https://example.com      -> ok: navigate
uv run agentic-browser-fleet state 0
  browser 0 @ https://example.com/  (Example Domain)
  Example Domain
  This domain is for use in illustrative examples...
  [18]<a /> Learn more
uv run agentic-browser-fleet click 0 18                      -> ok: click
uv run agentic-browser-fleet state 0     # re-state: the page is now iana.org
  browser 0 @ https://www.iana.org/help/example-domains  (Example Domains)
  ...
```

You opened a URL, asked the page what was on it, saw element `[18]` was the
"Learn more" link, clicked it, then re-ran `state` to see the new page.
Every clickable thing gets a `[number]` -- that number is what you pass to
`click`/`input`/`select`. **The numbers come from the last `state` and change
every time the page changes** (see "Always state before you click").

## Commands

Every command's **first argument is the browser id** (`0` is the default
browser). The fleet-level commands (`ls`, `new`) are the exception.

### Picking and making browsers

```bash
uv run agentic-browser-fleet ls
```

```text
browser 0: you -- 2 tab(s), active: https://example.com/invoices
browser 1: agent alice -- 1 tab(s), active: https://news.example.com
browser 2: human (took control) -- 1 tab(s), active: https://bank.example.com
browser 3: free -- 1 tab(s), active: (no tab)
```

`ls` shows the whole fleet: each browser's id, who controls it (`you`,
`agent <name>`, `human (took control)`, or `free`), tab count, and the
active tab's URL -- so you can pick one. Add `--include-tabs` to list every
tab of every browser:

```bash
uv run agentic-browser-fleet ls --include-tabs
#     [0]* Invoices            https://example.com/invoices
#     [1]  Dashboard           https://example.com/home
```

`new` starts another browser and prints its id:

```bash
uv run agentic-browser-fleet new        # -> started browser 4
```

If there are no browsers yet, `ls` tells you to `new` or just `state 0`
(running any command on browser 0 starts it).

### Looking at the page

```bash
uv run agentic-browser-fleet state 0
```

Prints `browser 0 @ <url>  (<title>)`, a `tabs:` line if more than one tab is
open, then the numbered interactive elements. If a page has no interactive
elements it prints `(no interactive elements -- try screenshot)`. **This is
your eyes. Run it constantly.**

```bash
uv run agentic-browser-fleet screenshot 0
# -> screenshot saved: /path/to/shot.png  (Read it to view)
```

`screenshot` saves a PNG and prints its path. Then **Read that path** with
your Read tool to actually *see* the page -- use this for visual layouts,
canvas, charts, captchas, or anything `state`'s text list can't convey.

### Acting on the page

| Command | What it does |
|---|---|
| `open <id> <url>` | Navigate the browser to a URL. (`-> ok: navigate`) |
| `click <id> <index>` | Click the element with that index from the last `state`. (`-> ok: click`) |
| `input <id> <index> "text"` | Type text into the field at that index. (`-> ok: input`) |
| `select <id> <index> "value"` | Choose an option in a `<select>` dropdown by its visible text. (`-> ok: select`) |
| `scroll <id> [down|up] [--amount N]` | Scroll the page. Direction defaults to `down`; `--amount` is pixels (default 500). |
| `keys <id> "Enter"` | Send keyboard keys, e.g. `"Enter"`, `"Control+a"`, `"Tab"`. |

A typical fill-and-submit:

```bash
uv run agentic-browser-fleet state 0                          # find the field indices
uv run agentic-browser-fleet input 0 5 "alice@example.com"    # email field
uv run agentic-browser-fleet input 0 6 "hunter2"              # password field
uv run agentic-browser-fleet click 0 7                        # the "Log in" button
uv run agentic-browser-fleet state 0                          # re-state: did we land on the dashboard?
```

(Or, instead of clicking the button, `keys 0 "Enter"` to submit the focused form.)

### Tabs within one browser

```bash
uv run agentic-browser-fleet tab 0 list           # list this browser's tabs
uv run agentic-browser-fleet tab 0 new --url https://example.com/help
uv run agentic-browser-fleet tab 0 switch 1       # make tab index 1 active
uv run agentic-browser-fleet tab 0 close 2        # close tab index 2
```

`tab` (no action) defaults to `list`. After `switch`/`new`/`close` the active
page changed, so **run `state <id>` again** before clicking anything.

### Ownership commands

```bash
uv run agentic-browser-fleet acquire 0            # reserve browser 0 across commands
uv run agentic-browser-fleet acquire 0 --reclaim  # take it back from a human -- ONLY on their say-so
uv run agentic-browser-fleet release 0            # let it go (alias: unlock 0)
```

You usually don't need `acquire` -- your first command auto-acquires the
browser and you keep a sticky lease across subsequent commands (see
Ownership). `acquire` is for explicitly reserving a browser, or for queueing
behind / reclaiming one that's held. `release` (alias `unlock`) hands it back;
releasing one that wasn't yours prints `browser <id> was not yours to release`
and still exits `0`.

## Key rules (internalize these)

### 1. Choosing a browser

Every command takes the browser id first. Run `ls` (or `ls --include-tabs`)
to see the fleet and pick one; `new` makes a fresh one; `0` is the default.
Drive several browsers at once just by using different ids -- they're
independent.

### 2. Always `state` before you `click`

The `[number]` indices come from the **latest** `state` and are
**ephemeral** -- the page re-numbers its elements whenever it changes. So:

> **Re-run `state <id>` after every `open`, `click`, `select`, `scroll`, or
> `tab` command -- and after you regain control of a browser.** Requery the
> page before you act on it.

This isn't bureaucracy; it's how you avoid clicking the wrong thing. If you
click against a stale index, the CLI does **not** mis-click -- it refuses and
tells you to re-`state` first:

```text
uv run agentic-browser-fleet click 0 18
  that element index is stale -- run `state 0` again first      (exit 1)
```

Treat that error as "I forgot to look first" -- run `state 0`, find the
element again under its *new* number, and click that.

### 3. No API key needed

You are the reasoning. You're already authenticated as yourself, and
`state` / `open` / `click` / `input` / `select` / `scroll` / `keys` /
`screenshot` / `tab` are all deterministic mechanical operations -- no LLM,
no key. (Only the optional `task` fallback at the very end spins up a
browser-use agent that needs an API key.)

### 4. Ownership, and the human at the wheel

Every browser has exactly one controller; every command's output names the
owner. The rules:

- **You auto-acquire and hold a sticky lease.** Your first command on a
  browser acquires it, and it stays yours across the commands that follow.
  No manual `acquire`/`release` in the normal case.
- **A human can take control** from the UI at any time. Your *next* command
  then comes back:

  ```text
  lost control of browser 0 (you took over). Send me a message
  ("keep going", "resume") when you want me to continue.        (exit 2)
  ```

  This is **exit 2 (preempted)**. **STOP.** Do not retry, do not reclaim.
  Tell the user you lost control and wait. Resume **only** when they
  explicitly say so ("keep going" / "resume") -- and only then run
  `acquire <id> --reclaim` (or pass `--reclaim` on your next action). Never
  pass `--reclaim` on your own initiative; that's yanking the wheel from a
  human who's using it.

- **Agents never preempt each other.** A browser another agent holds returns:

  ```text
  browser 1 is held by another agent. Pick another browser, or
  `acquire 1` to queue for it.                                  (exit 3)
  ```

  Pick a different id, or `acquire 1` to queue.

- **An idle lease frees itself.** If you walk away from a browser for a while
  (~90s), the daemon auto-releases it so it isn't stuck to you forever. If a
  later command says you no longer hold it, just acquire it again.

### 5. The human can watch live; your trace is here

The browser shows up live in a minds tab that pulls in next to your chat, so
the human can watch you operate it in real time. But that tab is **viewer
only** -- your actual output (the `state` listings, the `ok:`/error lines,
the screenshot paths) is in **your CLI output, here in the chat**, not in the
tab. Read and relay the CLI output; don't tell the user to "check the tab"
for results.

### 6. Multiple browsers, multiple tabs, sub-agents

- **Multiple browsers:** just use different ids. They don't interfere.
- **Tabs:** `tab <id> ...` manages tabs *within* one browser.
- **Sub-agents:** if you launch a sub-agent (via `launch-task`) that will run
  `agentic-browser-fleet` itself, set `BROWSER_FLEET_ANCHOR=chat:<your-name>`
  in its environment so its browser panes open next to **your** chat (where
  the human is watching), not next to the sub-agent's own. `create_worker.py`
  has no flag to inject env vars, so put the export in the **task file** you
  write for the sub-agent:

  ```text
  ## Environment
  Before running any agentic-browser-fleet command, export:

      export BROWSER_FLEET_ANCHOR=chat:<your MNGR_AGENT_NAME>

  so the browser panes open next to the orchestrator's chat.
  ```

  If unset it still works -- the pane just opens next to the sub-agent's own
  chat. It's an ergonomics nicety for the human, not a correctness
  requirement.

## Exit codes -- branch on these

| Code | Name | Meaning | What to do |
|---|---|---|---|
| `0` | ok | The command succeeded. | Read the output; for `state`, decide your next click. |
| `1` | error | Command failed, or a **stale index** (you clicked before re-`state`ing). | Run `state <id>` again, find the element's new number, retry. For other errors, read the message. |
| `2` | preempted | A human took control. | **Stop.** Tell the user; resume only on their explicit say-so (then `--reclaim`). Never auto-retry. |
| `3` | busy | Held by a human, or held by another agent. | Human: ask the user (then `--reclaim` if they agree). Another agent: pick a different browser, or `acquire <id>` to queue. |
| `4` | timed-out | You waited (`task --max-wait`) and another agent still held it. | Try later, or pick a different browser. |
| `64` | usage | `MNGR_AGENT_ID` unset / bad arguments. | Run from inside an agent shell; fix the command. |
| `69` | no daemon | Can't reach the browser daemon. | The browser service isn't running -- report it; don't blindly retry. |

## Quick recipes

```bash
# Look, then act.
uv run agentic-browser-fleet state 0
uv run agentic-browser-fleet click 0 12
uv run agentic-browser-fleet state 0            # always re-state after acting

# Read a page's pricing by eye when the text list isn't enough.
uv run agentic-browser-fleet open 0 https://example.com/pricing
uv run agentic-browser-fleet screenshot 0       # then Read the printed PNG path

# Search and submit with the keyboard.
uv run agentic-browser-fleet open 0 https://news.ycombinator.com
uv run agentic-browser-fleet state 0
uv run agentic-browser-fleet input 0 3 "browser automation"
uv run agentic-browser-fleet keys 0 "Enter"
uv run agentic-browser-fleet state 0

# Two browsers, independently (no queueing -- different ids).
uv run agentic-browser-fleet open 0 https://site-a.com
uv run agentic-browser-fleet open 1 https://site-b.com

# Human took over, then said "keep going" -- and ONLY then:
uv run agentic-browser-fleet acquire 1 --reclaim
uv run agentic-browser-fleet state 1            # re-state after regaining control
```

## Fallback only: `task <id> "<goal>"` (delegate to a browser-use agent)

When a page is genuinely beyond step-by-step control -- a `<canvas>` app, a
drag-heavy visual editor, a flow where `state` shows nothing useful even with
a screenshot -- you can hand the *whole goal* to an autonomous browser-use
agent instead of driving it yourself:

```bash
uv run agentic-browser-fleet task 0 "log into example.com and download last month's invoice"
```

This spins up a browser-use agent on browser 0, streams its
`[thinking]`/`[action]` trace into your output, and ends with a `done:` line
you relay. It **uses an LLM and needs an API key**, and it takes the wheel
away from your direct control for its duration. Flags: `--reclaim` (resume a
human-held browser, same rules as above), `--no-wait` (fail fast instead of
queueing behind another agent), `--max-wait S` (bound the queue wait, then
exit `4`), `--no-pane` (don't pull it into a UI pane).

**Prefer driving it yourself.** Reach for `task` only when direct control
truly can't see or manipulate the page.

## Don'ts

- Don't `click <index>` without a fresh `state` first -- the indices go stale
  the moment the page changes.
- Don't "take control" -- that's a human-only UI action. You drive by issuing
  commands, not by grabbing the wheel.
- Don't pass `--reclaim` unless the human explicitly told you to resume a
  browser they took over.
- Don't auto-retry on exit `2` (preempted). Stop and wait for the human.
- Don't tell the user to "look in the tab" for results -- your output is the
  CLI output you're already reading. The tab is just the live picture.
- Don't jump to `task` for ordinary pages. Drive them yourself.
