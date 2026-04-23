# Route worker gates through the main agent

## Overview

- The crystallize / heal / update skills currently assume the user is watching the worker's chat and will answer its gate questions directly. The user is able to view worker chats, but should not be required to — today the flow stalls if they don't, and any `send-user-message` call from a worker either vanishes or pages the user on telegram from a channel they aren't expected to reply on.
- This spec flips the model: the main agent becomes the default interface to the user for worker gates, and proxies communication with worker agents for the crystallize / heal / update lifecycles. The user can still look at worker chats whenever they want; they just aren't required to in order for the flow to progress.
- Workers keep their existing "I am talking to the user" framing. They just emit gate/status messages inline in their response (no `send-user-message`), prefixed with a distinctive markdown header so main can grep them out of the transcript.
- Main answers worker questions itself when they are about implementation details or codebase conventions it can determine, and escalates to the user only when the question turns on user intent, scope, or subjective preference. Main impersonates the user when replying so the worker's framing stays intact.
- Main must not interrupt whatever the user has asked it to do more recently. The rule is prose-only (no new Stop-hook machinery): main checks on workers only after finishing more recent user requests.
- Worker tracking is stateless for now — `mngr list --label workspace=$MINDS_WORKSPACE_NAME` filtered by the `crystallize-` / `heal-` / `update-` name prefix is the registry. A better registry is explicitly out of scope.

## Expected behavior

- A crystallize / heal / update flow kicked off by the user results in main launching a worker, then main (not the user) shepherds it to completion.
- When the worker reaches a gate, its response begins with `## GATE: <gate-name>` (e.g. `## GATE: outline-approval`, `## GATE: final-artifact`). When the worker is terminal, its response begins with `## STATUS: <status>` (e.g. `## STATUS: done`, `## STATUS: stuck`, `## STATUS: no-update-needed`).
- `mngr wait <worker> DONE STOPPED WAITING` run in the background notifies main when the worker transitions. Main grabs the latest transcript message, looks for a leading `## GATE:` or `## STATUS:` line, and branches on the result.
- For `## GATE:` responses, main decides:
  - If the question is answerable from code / docs / conventions / prior context main already has: main answers it via `mngr message <worker> -m "<answer>"`, phrased as if it were the user. Main then re-launches `mngr wait` in the background and continues with whatever else it was doing.
  - Otherwise: main surfaces the question to the user via `send-user-message` (which dispatches to telegram or inline as configured), waits for the user's reply on its own channel, forwards the reply via `mngr message`, and re-launches `mngr wait`.
- For `## STATUS: done` responses, main merges the worker branch as before and closes the tracking ticket.
- For `## STATUS: stuck` or a worker that finished without a recognized marker, main follows `launch-task/references/worker-failure.md`: capture, tell the user, leave the branch intact.
- If the user gives main a new task while a worker is mid-gate, main does NOT drop that task to handle the worker. Main finishes the user's current request first, then checks for any still-outstanding `mngr wait` background notifications it hasn't handled.
- Workers run with their existing skills and their existing mental model. Only the instructions about how to format gate/status messages change; workers still think they're addressing the user.
- The Stop-hook crystallization nudge (`scripts/detect_crystallization_candidate.py`) is unchanged — no new hook is added.

## Changes

- **`.agents/skills/crystallize-task/SKILL.md`**
  - Remove the "user will interact with the worker directly" framing (currently in Step 3's task file template and Step 5).
  - Document the proxy model: after launching, main loops on `mngr wait` notifications, scrapes the latest assistant message for `## GATE:` / `## STATUS:` markers, and decides answer-locally vs. escalate-to-user.
  - Spell out the decision rule for answer-locally vs. escalate (prose guideline: implementation details / conventions → main answers; user intent / scope / subjective preference → escalate).
  - Add the "don't interrupt in-progress user work" rule explicitly.
  - After forwarding a user's reply via `mngr message`, re-launch `mngr wait` in the background so the next gate is caught.

- **`.agents/skills/heal-skill/SKILL.md`** and **`.agents/skills/update-skill/SKILL.md`**
  - Same set of changes as above, adapted to their Gate 2 (and Gate 1 for update-skill) flows.
  - Remove the "user will handle gate approval directly with the worker" language in Step 5.

- **`.agents/skills/crystallize-task/assets/worker-skills/crystallize-task-worker/SKILL.md`**
  - Rewrite the Gate 1 (outline approval) and Gate 2 (final artifact approval) sections so the worker emits the gate message inline in its response, starting with `## GATE: outline-approval` / `## GATE: final-artifact`. Remove the `send-user-message` framing if it exists.
  - Rewrite the terminal messages (Stage 7 handoff, give-up) to start with `## STATUS: done` or `## STATUS: stuck`.
  - Keep the worker's existing "I am asking the user" framing in the gate's prose body — only the header and the delivery mechanism change.

- **`.agents/skills/crystallize-task/assets/worker-skills/heal-skill-worker/SKILL.md`**
  - Same changes for Gate 2 (`## GATE: final-artifact`), terminal (`## STATUS: done`), and the "could not heal" exit (`## STATUS: stuck`).

- **`.agents/skills/crystallize-task/assets/worker-skills/update-skill-worker/SKILL.md`**
  - Same changes for Gate 1 (`## GATE: outline-approval`), Gate 2 (`## GATE: final-artifact`), terminal (`## STATUS: done`), and the "no update needed" exit (`## STATUS: no-update-needed`).

- **`.agents/skills/launch-task/references/worker-failure.md`**
  - Add a short section mapping the new `## STATUS: stuck` marker to the existing failure-handling flow (capture transcript, tell user, leave branch intact), so main treats it consistently across skills.

- **`CLAUDE.md` (top-level)**
  - Add a short subsection under "Work delegation" (or adjacent to it) stating: the user can view worker chats but is not required to; when main launches workers via crystallize / heal / update, main is responsible for answering gate questions itself when feasible and escalating to the user only for genuinely ambiguous ones; main must not interrupt more recent user work to handle a worker event.

- No changes to:
  - `scripts/detect_crystallization_candidate.py` (Stop hook stays as-is).
  - `.claude/settings.json` hooks block.
  - Any `mngr` code — `mngr wait` / `mngr message` / `mngr transcript` already do what we need.
  - The `send-user-message` skill (still used by main → user communication; only workers stop calling it for gates).
