# Hardening an artifact

The universal contract for the **background harden pass**: once the user has
signed off on a shape in the foreground, put in the thorough, expensive effort
to turn it into a hardened, committed, reviewed artifact -- in the background,
off the interactive path. This contract is the part that is identical across
every operation (crystallize, update, heal) and every artifact (a reusable
skill, a web service, the system interface).

## The premise and the bar

The user has already signed off on work in the foreground; the thorough pass has been **deliberately deferred**.
The task now is to prove the artifact actually works under test, harden it, and pass the review
gates. The bar is that the artifact is **genuinely well-tested and clean** -- not "it ran once."

## Isolation

Do all of this on an **isolated branch / worktree**. Nothing should the
live, user-facing state until the branch is merged. If the worktree has
no `.venv`, sync once before any `uv run`. If a fix needs a new dependency, add
it the normal way and commit the manifest changes so they appear in the merge.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and the task-file frontmatter schema, and substitute the runtime
paths your operation/artifact references specify. Surface decisions the user
must make as `gate` reports and stop; end the run with a terminal `done` or
`stuck` status. The operation reference names the exact gate and
status values its flow uses.

## Testing and hardening contract

- **Write or extend thorough tests** that assert on markers which are true if
  and only if the artifact behaves correctly -- not just that it ran. Cover the
  real behavior, including empty and overflow states.
- **Add fixture-based tests for anything that parses external data** (HTML, JSON
  from third-party APIs, scraped pages, uploaded files). Live-data checks alone
  miss the class of bugs that only surface when a specific input shape hits the
  parser. Save 1-3 representative samples as fixtures and assert on the exact
  parsed shape.
- Keep behavior worth re-checking as committed tests; use ad-hoc manual checks
  only for purely visual things not worth a permanent test, and do not duplicate
  the same coverage in both.
- **Run every suite that applies** plus the relevant ratchets.

## Optimize independent work -- parallelize what the incremental pass left serial

The foreground pass optimizes for getting *something* working, so it tends to
leave independent operations running one after another. The harden pass is where
you make the artifact fast, since the expensive rethink is deliberately deferred
to here. **When the artifact performs multiple independent I/O-bound operations
-- data fetches, API calls, subprocess invocations -- that do not depend on each
other's results, run them concurrently rather than in a serial loop.** This is
most impactful for fetch-heavy pipelines (the fetch-process-show shape), where a
serial loop over many sources is often the dominant cost and parallelizing it is
a large, cheap win.

Guardrails:

- **Bound the concurrency** and respect the provider's rate limits -- an
  unbounded fan-out that trips throttling or bans is slower and worse than the
  serial version. Use a semaphore / worker pool sized to what the source allows.
- **Only parallelize genuinely independent work.** Keep steps serial where one
  depends on another's output, where ordering matters, or where the source
  requires sequential access (cursor pagination, stateful sessions).
- Preserve deterministic output: collect concurrent results and order them
  explicitly rather than relying on completion order, so tests and surfaces stay
  stable.

## Tolerate partial failure and support resumption

A pipeline that touches many external sources will eventually hit a failure on
one of them -- a timeout, a rate-limit, a malformed record. The harden pass is
where you make the artifact survive that gracefully instead of crashing. This
applies to any multi-item pipeline, but parallel fan-out makes it urgent, since
more operations in flight means more failure surface.

- **Isolate failures -- one failed operation must not sink the whole run.**
  Capture each item's outcome (result or error) independently, continue past a
  single failure, and report which items failed and why. A partial result over
  the sources that succeeded is far more useful than a crash that discards the
  ones that worked; surface the failures so the user knows the result is partial
  rather than silently dropping them.
- **Persist results incrementally so a run can resume.** Write each result to
  durable storage as it completes rather than accumulating everything in memory
  and saving at the end -- then a re-run (after a crash, a rate-limit pause, or a
  transient failure) picks up from what already landed instead of refetching
  from scratch. This dovetails with the preserve-and-surface contract below:
  persist the raw payload keyed by its source id, so the same store serves both
  resumption and later re-derivation.

## Review gates

Run the repo's review gates -- `/autofix` and the architecture gates -- and
fix what they flag **before** writing the final gate report, so the user sees
a single report that already reflects the review verdicts rather than a
report-then-verify-then-report-again pattern.

Autofix's normal final step asks the user to keep or revert each proposed fix
via AskUserQuestion, which is unavailable in a worker -- so split that decision
out and make it yourself. Invoke autofix so it *applies* its fixes but leaves
the keep/revert judgment to you:

    /autofix Run fully unattended: never call AskUserQuestion. Run the fix
    loop, leave every fix commit applied, and report the fix commits (hash +
    full message). Do not revert anything yourself -- the caller will decide.

Then review those fix commits against what this branch is meant to do. You hold
the task context the fix subagents run without, so you are the right judge of
whether each fix is correct. Keep fixes by default; revert only the ones that
undo intended behavior or are otherwise wrong (`git revert --no-edit <hash>`,
newest first). Record which you kept and which you reverted in your gate report.

## Preserve and surface captured data

If the artifact captures data, persist each record's **raw payload and a
reference to its source, durably** -- not just the extracted/processed fields
(see the preserve-and-surface principle in CLAUDE.md). A pipeline that fetches,
transforms, and discards the raw payload cannot satisfy that principle no matter
what consumers do: persisting it is what lets a later change in processing
re-derive new fields with no refetch, and what lets surfaces show the raw record
or link out to its source. Retain whatever a consumer needs to render the record
faithfully later.

## Bound disk growth -- evict what the artifact no longer needs

This is an always-applies invariant, not a creation-time step. It holds
**whenever** the artifact persists anything across runs -- when you first build
it, and equally when an `update` teaches a previously stateless skill or service
to fetch or store, or enlarges a store it already had. The persist-and-preserve
contracts above tell you to write records durably and incrementally. Left
unqualified, that turns any artifact you run more than once -- a recurring
pipeline, a service that polls on a schedule, anything that appends results, raw
payloads, logs, caches, or fixtures -- into an ever-growing store that
eventually fills the disk. The harden pass is where you give every growing store
a **bounded retention policy**; an artifact that can only accrete and never
evicts is not hardened, no matter how well-tested its happy path is.

- **Find every store the artifact writes to and decide, explicitly, what bounds
  each one.** Persisted results, raw-payload archives, on-disk caches, log
  files, generated fixtures, temp/scratch directories -- for each, pick a bound
  (a max age, a max count, a max total size, or "keep only the latest N runs")
  and enforce it as part of the artifact's own flow, not as a chore you hope
  someone remembers.
- **Evict as part of the same run that writes.** Prune on write (or on a step
  the run always reaches) so the bound holds without a separate cleanup process.
  A retention policy that depends on out-of-band manual cleanup is not a bound.
- **Reconcile eviction with preserve-and-surface, don't skip it.** These pull in
  opposite directions on purpose: keep the raw source records long enough to
  re-derive and surface them, but cap how far back that retention goes. When a
  record ages out, evict the derived data and its raw payload together so a
  surface never points at a source that has been pruned out from under it.
- **Prefer existing rotation/TTL machinery over hand-rolled deletion.** Use log
  rotation, a store's built-in TTL/size cap, or the repo's existing retention
  helpers before writing your own prune loop -- and never delete by a fragile
  heuristic (glob-and-`rm`) that could take out data the artifact still needs.
- **Cover the bound with a test.** Assert that after writing past the limit the
  store holds only what the policy allows and that the oldest entries are gone --
  the eviction path is exactly the kind of behavior that silently rots if
  nothing exercises it.

## If you need to give up

If you cannot reach a tested, clean state (a dependency you cannot resolve, an
intended behavior you cannot pin down from the task file), emit a `stuck`
terminal report stating what blocked you and where the work stands. Do not
report `done` on an artifact whose tests or gates do not pass. "Too
judgement-heavy" is never a valid reason to give up -- model judgement that is a
fixed part of the flow is scripted, not abandoned; only give up if the process
itself is unstable.
