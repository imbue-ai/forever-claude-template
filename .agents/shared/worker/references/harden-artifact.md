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

## Review gates

Run the repo's review gates -- `/autofix` and the architecture gates -- and
fix what they flag **before** writing the final gate report, so the user sees
a single report that already reflects the review verdicts rather than a
report-then-verify-then-report-again pattern.

## Preserve and surface captured data

If the artifact captures data, persist each record's **raw payload and a
reference to its source, durably** -- not just the extracted/processed fields
(see the preserve-and-surface principle in CLAUDE.md). A pipeline that fetches,
transforms, and discards the raw payload cannot satisfy that principle no matter
what consumers do: persisting it is what lets a later change in processing
re-derive new fields with no refetch, and what lets surfaces show the raw record
or link out to its source. Retain whatever a consumer needs to render the record
faithfully later.

## If you need to give up

If you cannot reach a tested, clean state (a dependency you cannot resolve, an
intended behavior you cannot pin down from the task file), emit a `stuck`
terminal report stating what blocked you and where the work stands. Do not
report `done` on an artifact whose tests or gates do not pass. "Too
judgement-heavy" is never a valid reason to give up -- model judgement that is a
fixed part of the flow is scripted, not abandoned; only give up if the process
itself is unstable.
