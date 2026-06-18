---
name: inbox-digest
description: >-
  Fetch the user's unread Gmail (query configurable), classify each email by
  type, and extract the useful information per type into a flat, structured
  digest -- newsletters become article lists, GitHub notifications become CI
  run summaries, and action items / events / receipts / recruiter messages get
  their key fields pulled out, while promotions collapse to one-liners. Each
  record keeps the raw email body and a link back to Gmail so a view can render
  the original. Use it to skim the inbox without opening individual emails, or
  to refresh the digest a web surface reads.
metadata:
  crystallized: true
---

# inbox-digest

Turn an unread Gmail inbox into a flat, scan-and-done digest: one structured
record per email, classified by type, with the information extracted so the
reader never has to open the original.

## Prerequisites

`latchkey` must have a valid `google-gmail` credential with the
`google-gmail-read-all` permission (already approved for this user). If a fetch
returns a permission error, request it via the `latchkey` skill.

This deployment is **keyless** (no `ANTHROPIC_API_KEY`), so the classification
step uses the `claude -p` helper (`scripts/claude_p.py`). If a key is later
added, switch the AI call in `scripts/run.py` to a direct `litellm` completion
per the `use-ai-integration` skill -- nothing else changes.

## Pipeline

Three steps, each a subcommand with an inspectable intermediate artifact, plus
a `run-all` that chains them for headless/scheduled runs.

1. **`fetch`** `[script]` -- list messages matching the query, fetch each with
   full content, decode the body (text/plain preferred, else text/html stripped
   to text), and persist both the raw Gmail payloads (`messages/<id>.json`) and
   the decoded records (`raw.json`). Captures all reasonable per-message fields
   (labels, snippet, thread id), not just what the digest displays.
2. **`digest`** `[ai-script]` -- one Claude call per email
   (`claude-haiku-4-5`) that both classifies the email into a category and
   extracts that category's fields, returning JSON. Calls run concurrently. The
   script injects the preserved fields (`id`, `from`, `subject`, `date`,
   `gmail_url`, `raw_body`) so the model never overrides the source of truth,
   and writes `digest.json`. Total AI cost prints to stderr.
3. **`run-all`** `[script]` -- `fetch` then `digest` in-process, then refresh
   the stable copy at `runtime/inbox-digest/digest.json` (same schema) that the
   web surface reads, and print the digest to stdout.

There are no user-in-the-loop (`[prose]`) steps: the whole flow runs unattended.

## Usage

```bash
# End-to-end (headless): fetch unread inbox, digest, refresh the stable copy.
uv run .agents/skills/inbox-digest/scripts/run.py run-all

# A different query / cap, and a chosen run directory.
uv run .agents/skills/inbox-digest/scripts/run.py run-all \
    --query "is:unread in:inbox newer_than:2d" --max 100 \
    --out-dir runtime/inbox-digest/manual

# Step by step (richer progress when run from a chat turn).
uv run .agents/skills/inbox-digest/scripts/run.py fetch  --out-dir runtime/inbox-digest/run1
uv run .agents/skills/inbox-digest/scripts/run.py digest --out-dir runtime/inbox-digest/run1
```

`fetch` and `run-all` default `--out-dir` to a timestamped directory under
`runtime/inbox-digest/` so history is preserved; `run-all` always also writes
`runtime/inbox-digest/digest.json` (override with `--stable-path`).

## Output schema

`digest.json` is a JSON list. Every record has:

```
id, category, from, subject, date, gmail_url, raw_body, sender_name
```

`category` is one of `newsletter`, `github`, `event`, `action`, `receipt`,
`networking`, `promotion`, and adds that category's fields:

- **newsletter** -- `digest_title`, `items[]` (`title`, `read`, `summary`,
  `link`); `is_satire: true` for satire (e.g. The Onion). Ground News uses the
  source count for `read` and appends the L/C/R bias spread to `summary`.
- **github** -- `repo`, `workflow`, `pr_title`, `status`, `failed_jobs[]`,
  `link`.
- **event** -- `digest_title`, `events[]` (`what`, `when`, `where`, optional
  `link`/`change`/`rsvp_link`); optional `rsvp`, `attendance`.
- **action** -- `action`, `details`, `due`, optional `link`.
- **receipt** -- `details`, `amount`, `link`.
- **networking** -- `details`, `reply_link`.
- **promotion** -- `one_liner`.

Footnote links (`[6]`, `[7]`...) in newsletters are resolved by the AI from the
body's link list. Bodies are preserved untruncated in `raw_body` and the raw
Gmail payloads; for very long bodies the AI receives head + tail so the
footnote table at the bottom is always included.

## Preserve and surface

The raw Gmail payload of every email is kept at `messages/<id>.json` and the
full decoded body at `raw_body`, with `gmail_url` linking back to the source.
A later change in what the digest extracts needs no refetch, and a surface can
render the original email or jump to Gmail.

## Tests

`scripts/run_test.py` is a fixture-based unit test for the deterministic body
parser (multipart text/plain-preferred, html-only stripping, base64url
padding), using synthetic Gmail `format=full` payloads under `tests/fixtures/`.
Run it with `uv run --with pytest pytest .agents/skills/inbox-digest/scripts/run_test.py`.
The `digest` step makes real AI calls; exercise it on a small `raw.json` and
inspect `digest.json` rather than asserting exact model output.
