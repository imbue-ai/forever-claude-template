#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["anyio", "pydantic>=2"]
# ///
"""inbox-digest: fetch unread Gmail, classify each email, extract per-type info.

Three subcommands, each a clean step boundary, plus ``run-all`` that chains them:

  fetch    -- list messages for a query, fetch each full payload via latchkey,
              decode the body, persist raw payloads + raw.json.
  digest   -- classify + extract per-category fields for each raw record via
              one Claude call per email (keyless ``claude -p``), write digest.json.
  run-all  -- fetch then digest in-process; also refresh the stable copy at
              runtime/inbox-digest/digest.json that the web surface reads.

Per-email classification/extraction is a model-judgement step scripted as an AI
call (see the use-ai-integration skill); only the data varies between runs.

This deployment is keyless (no ANTHROPIC_API_KEY), so the AI step uses the
``claude -p`` helper in claude_p.py. If a key is later added, switch the AI call
to a direct litellm completion per the use-ai-integration skill.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_p import claude_p_completion  # noqa: E402  (copied helper, same dir)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_QUERY = "is:unread in:inbox"
DEFAULT_MAX = 60
DEFAULT_ROOT = Path("runtime/inbox-digest")
STABLE_DIGEST_PATH = DEFAULT_ROOT / "digest.json"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
AI_MODEL = "claude-haiku-4-5"
# Cap how much body text goes into the AI prompt. Newsletter footnote-link
# tables live at the *bottom* of the body, so when a body exceeds the cap we
# keep the head AND tail rather than truncating from the end.
PROMPT_BODY_HEAD = 12000
PROMPT_BODY_TAIL = 6000
# How many AI calls to run concurrently (each is a claude -p subprocess).
AI_CONCURRENCY = 6

CATEGORIES = (
    "newsletter",
    "github",
    "event",
    "action",
    "receipt",
    "networking",
    "promotion",
)
# Fields the script owns; the AI must never set these (we inject them from the
# fetched record so the source of truth stays the raw email, not the model).
PRESERVED_KEYS = frozenset({"id", "from", "subject", "date", "gmail_url", "raw_body"})


class InboxDigestError(RuntimeError):
    """A step of the pipeline failed in a way the caller should see."""


# ---------------------------------------------------------------------------
# Gmail fetch (deterministic)
# ---------------------------------------------------------------------------


def _latchkey_get(url: str) -> dict[str, object]:
    """GET a Gmail API URL through ``latchkey curl`` and parse the JSON body."""
    proc = subprocess.run(
        ["latchkey", "curl", "-s", url],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise InboxDigestError(
            f"latchkey curl failed ({proc.returncode}) for {url}: "
            f"{proc.stderr.strip()[:300]}"
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise InboxDigestError(
            f"Gmail response was not JSON for {url}: {proc.stdout[:300]}"
        ) from exc
    if isinstance(data, dict) and "error" in data:
        raise InboxDigestError(f"Gmail API error for {url}: {data['error']}")
    if not isinstance(data, dict):
        raise InboxDigestError(f"Gmail response was not a JSON object for {url}")
    return data


def list_message_ids(query: str, max_results: int) -> list[str]:
    """List message ids matching ``query`` (paginating up to ``max_results``)."""
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < max_results:
        page_size = min(100, max_results - len(ids))
        url = f"{GMAIL_API}/messages?maxResults={page_size}&q={quote(query)}"
        if page_token:
            url += f"&pageToken={quote(page_token)}"
        data = _latchkey_get(url)
        for msg in data.get("messages", []) or []:
            if isinstance(msg, dict) and "id" in msg:
                ids.append(str(msg["id"]))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        page_token = str(next_token)
    return ids[:max_results]


def fetch_message(message_id: str) -> dict[str, object]:
    """Fetch a single message with full content."""
    return _latchkey_get(f"{GMAIL_API}/messages/{quote(message_id)}?format=full")


class _HTMLToText(HTMLParser):
    """Minimal HTML-to-text: drop tags, keep text, turn block tags into breaks."""

    _BLOCK = {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "ul", "ol"}
    _SKIP = {"style", "script", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # collapse runs of blank lines / trailing spaces left by block breaks
        lines = [line.strip() for line in joined.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def strip_html(html: str) -> str:
    """Render HTML email content down to readable plain text."""
    parser = _HTMLToText()
    parser.feed(html)
    return parser.text()


def _decode_b64url(data: str) -> str:
    """Decode a Gmail base64url body part to text (Gmail omits padding)."""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InboxDigestError(f"could not base64url-decode a body part: {exc}") from exc
    return raw.decode("utf-8", errors="replace")


def _collect_parts(payload: dict[str, object]) -> dict[str, str]:
    """Walk a MIME tree, returning decoded text keyed by mime type (plain/html)."""
    found: dict[str, str] = {}

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        mime = str(node.get("mimeType", ""))
        body = node.get("body")
        if isinstance(body, dict) and isinstance(body.get("data"), str):
            text = _decode_b64url(body["data"])
            if mime == "text/plain" and "plain" not in found:
                found["plain"] = text
            elif mime == "text/html" and "html" not in found:
                found["html"] = text
        for child in node.get("parts", []) or []:
            walk(child)

    walk(payload)
    return found


def extract_body(payload: dict[str, object]) -> tuple[str, str]:
    """Return ``(body_kind, text)`` for a message payload.

    Prefers text/plain; falls back to text/html stripped to text. ``body_kind``
    is "plain", "html", or "none" so a downstream surface knows how the email
    originally rendered.
    """
    parts = _collect_parts(payload)
    if parts.get("plain", "").strip():
        return "plain", parts["plain"]
    if parts.get("html", "").strip():
        return "html", strip_html(parts["html"])
    return "none", ""


def _header(payload: dict[str, object], name: str) -> str:
    for entry in payload.get("headers", []) or []:
        if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
            return str(entry.get("value", ""))
    return ""


def to_raw_record(message: dict[str, object]) -> dict[str, object]:
    """Project a full Gmail message into the decoded record the digest consumes.

    Captures all reasonable per-message fields (labels, snippet, thread) so a
    later consumer is not constrained to what today's digest displays.
    """
    payload = message.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    body_kind, body = extract_body(payload)
    message_id = str(message.get("id", ""))
    return {
        "id": message_id,
        "threadId": str(message.get("threadId", "")),
        "labelIds": list(message.get("labelIds", []) or []),
        "snippet": str(message.get("snippet", "")),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "gmail_url": f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
        "body_kind": body_kind,
        "body": body,
    }


def fetch(query: str, max_results: int, out_dir: Path) -> list[dict[str, object]]:
    """Fetch matching messages, persist raw payloads + raw.json, return records."""
    out_dir.mkdir(parents=True, exist_ok=True)
    messages_dir = out_dir / "messages"
    messages_dir.mkdir(exist_ok=True)

    ids = list_message_ids(query, max_results)
    records: list[dict[str, object]] = []
    for message_id in ids:
        message = fetch_message(message_id)
        # Preserve the raw payload (source of truth) before any derivation.
        (messages_dir / f"{message_id}.json").write_text(
            json.dumps(message, indent=1, ensure_ascii=False)
        )
        records.append(to_raw_record(message))

    (out_dir / "raw.json").write_text(json.dumps(records, indent=1, ensure_ascii=False))
    return records


# ---------------------------------------------------------------------------
# Classify + extract (AI)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an email triage assistant. You read one email and return a single JSON
object that classifies it and extracts the information a reader needs so they
never have to open the original email.

Return ONLY the JSON object -- no prose, no markdown fences.

Pick exactly one "category" from:
  newsletter, github, event, action, receipt, networking, promotion.

Always include:
  "category": one of the values above.
  "sender_name": a short human-readable sender label (e.g. "TLDR AI",
                 "American Airlines", "LinkedIn -- recruiter InMail").

Then add ONLY the fields for the chosen category:

newsletter (a digest of articles/links):
  "digest_title": short label for the newsletter
                  (e.g. "TLDR AI -- daily AI roundup").
  "items": array of objects, one per notable article, each:
     "title": the article headline.
     "read": reading time if stated (e.g. "4 min"); else a short label
             ("short", "feature"). For Ground News use the source count
             (e.g. "165 sources").
     "summary": one concise line. For Ground News append the left/center/right
                bias spread (e.g. " Coverage: 30% L / 54% C / 16% R.").
     "link": the real article URL. Newsletters reference links with footnote
             markers like [6]; resolve each marker to its URL using the link
             list in the body. If a link cannot be resolved, use the gmail_url
             given below.
  "is_satire": true ONLY for satire publications (e.g. The Onion); otherwise
               omit this field entirely.

github (a GitHub Actions / CI notification):
  "repo", "workflow", "pr_title", "status" (e.g. "failed", "succeeded"),
  "failed_jobs": array of failed job names (empty array if none),
  "link": the workflow-run URL.

event (concert/show/calendar listings):
  "digest_title": short label.
  "events": array of objects, each "what", "when", "where", and optionally
            "link", "change" (e.g. "Time changed"), "rsvp_link".
  optionally "rsvp": true for calendar invites; "attendance": e.g. "optional".

action (something the reader must do or act on -- flight changes, secure
        messages to read, address/order confirmations, deadlines):
  "action": a short imperative summary of what to do.
  "details": all specifics needed to act without opening the email.
  "due": the deadline/timeframe (e.g. "Today", "Within 72 hours", or "--").
  optionally "link": the URL to act on.

receipt (a payment/purchase confirmation):
  "details": one line (merchant, amount, card, date).
  "amount": the charged amount (e.g. "$11.08").
  "link": the full-receipt URL.

networking (recruiter outreach / LinkedIn InMail):
  "details": a summary of the message and the offer.
  "reply_link": the reply/thread URL.

promotion (marketing / sales / offers -- lowest priority):
  "one_liner": a single concise line capturing the offer.
"""


def _build_user_prompt(record: dict[str, object]) -> str:
    body = str(record.get("body", ""))
    if len(body) > PROMPT_BODY_HEAD + PROMPT_BODY_TAIL:
        body = (
            body[:PROMPT_BODY_HEAD]
            + "\n\n...[body truncated; tail follows]...\n\n"
            + body[-PROMPT_BODY_TAIL:]
        )
    return (
        f"From: {record.get('from', '')}\n"
        f"Subject: {record.get('subject', '')}\n"
        f"Date: {record.get('date', '')}\n"
        f"gmail_url (link fallback): {record.get('gmail_url', '')}\n"
        f"\n--- EMAIL BODY ---\n{body}\n"
    )


def _parse_ai_json(text: str) -> dict[str, object]:
    """Extract the JSON object from an AI response (tolerating stray fences)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # drop a leading ```json / ``` fence and the trailing ```
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise InboxDigestError(f"AI response had no JSON object: {text[:200]}")
    try:
        data = json.loads(cleaned[start : end + 1])
    except ValueError as exc:
        raise InboxDigestError(f"AI JSON did not parse: {exc}; text={text[:200]}") from exc
    if not isinstance(data, dict):
        raise InboxDigestError("AI JSON was not an object")
    return data


def _merge_record(record: dict[str, object], extracted: dict[str, object]) -> dict[str, object]:
    """Build the final digest record: preserved fields + AI-extracted fields."""
    category = str(extracted.get("category", "")).strip()
    if category not in CATEGORIES:
        raise InboxDigestError(
            f"AI returned unknown category {category!r} for message "
            f"{record.get('id')!r}"
        )
    digest: dict[str, object] = {
        "id": record.get("id", ""),
        "category": category,
        "from": record.get("from", ""),
        "subject": record.get("subject", ""),
        "date": record.get("date", ""),
        "gmail_url": record.get("gmail_url", ""),
        "raw_body": record.get("body", ""),
    }
    # Layer AI fields on top, but never let the model overwrite preserved keys
    # or restate the category we already validated.
    for key, value in extracted.items():
        if key in PRESERVED_KEYS or key == "category":
            continue
        digest[key] = value
    return digest


@dataclass
class _DigestOutcome:
    records: list[dict[str, object]]
    cost_usd: float


async def _classify_one(
    record: dict[str, object],
    index: int,
    results: list[dict[str, object] | None],
    costs: list[float],
    limiter: anyio.Semaphore,
) -> None:
    async with limiter:
        result = await claude_p_completion(
            _build_user_prompt(record),
            system=_SYSTEM_PROMPT,
            model=AI_MODEL,
        )
    extracted = _parse_ai_json(result.text)
    results[index] = _merge_record(record, extracted)
    costs[index] = result.cost_usd


async def _digest_async(records: list[dict[str, object]]) -> _DigestOutcome:
    results: list[dict[str, object] | None] = [None] * len(records)
    costs: list[float] = [0.0] * len(records)
    limiter = anyio.Semaphore(AI_CONCURRENCY)
    async with anyio.create_task_group() as tg:
        for index, record in enumerate(records):
            tg.start_soon(_classify_one, record, index, results, costs, limiter)
    finished = [record for record in results if record is not None]
    return _DigestOutcome(records=finished, cost_usd=sum(costs))


def digest(records: list[dict[str, object]], out_dir: Path) -> list[dict[str, object]]:
    """Classify + extract every record via AI, write digest.json, return records."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not records:
        outcome = _DigestOutcome(records=[], cost_usd=0.0)
    else:
        outcome = anyio.run(_digest_async, records)
    (out_dir / "digest.json").write_text(
        json.dumps(outcome.records, indent=1, ensure_ascii=False)
    )
    print(
        f"Digested {len(outcome.records)}/{len(records)} emails "
        f"(AI cost ${outcome.cost_usd:.4f}).",
        file=sys.stderr,
    )
    return outcome.records


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _default_out_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_ROOT / stamp


def _write_stable_copy(records: list[dict[str, object]], stable_path: Path) -> None:
    """Refresh the fixed-path digest the web surface reads (same schema)."""
    stable_path.parent.mkdir(parents=True, exist_ok=True)
    stable_path.write_text(json.dumps(records, indent=1, ensure_ascii=False))


def run_all(query: str, max_results: int, out_dir: Path, stable_path: Path) -> None:
    records = fetch(query, max_results, out_dir)
    digested = digest(records, out_dir)
    _write_stable_copy(digested, stable_path)
    json.dump(digested, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_records(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise InboxDigestError(f"{path} is not a JSON list of records")
    return data


def _cmd_fetch(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir) if args.out_dir else _default_out_dir()
    records = fetch(args.query, args.max, out_dir)
    print(f"Fetched {len(records)} emails -> {out_dir / 'raw.json'}", file=sys.stderr)


def _cmd_digest(args: argparse.Namespace) -> None:
    if not args.raw and not args.out_dir:
        raise InboxDigestError("digest needs --raw PATH or --out-dir DIR (with raw.json)")
    raw_path = Path(args.raw) if args.raw else Path(args.out_dir) / "raw.json"
    out_dir = Path(args.out_dir) if args.out_dir else raw_path.parent
    records = _load_records(raw_path)
    digested = digest(records, out_dir)
    json.dump(digested, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


def _cmd_run_all(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir) if args.out_dir else _default_out_dir()
    run_all(args.query, args.max, out_dir, Path(args.stable_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="fetch + persist raw Gmail records")
    p_fetch.add_argument("--query", default=DEFAULT_QUERY)
    p_fetch.add_argument("--max", type=int, default=DEFAULT_MAX)
    p_fetch.add_argument("--out-dir", default=None, help="default: timestamped dir")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_digest = sub.add_parser("digest", help="classify + extract from raw records")
    p_digest.add_argument("--raw", default=None, help="path to raw.json")
    p_digest.add_argument("--out-dir", default=None, help="where digest.json lands")
    p_digest.set_defaults(func=_cmd_digest)

    p_all = sub.add_parser("run-all", help="fetch then digest (headless)")
    p_all.add_argument("--query", default=DEFAULT_QUERY)
    p_all.add_argument("--max", type=int, default=DEFAULT_MAX)
    p_all.add_argument("--out-dir", default=None, help="default: timestamped dir")
    p_all.add_argument("--stable-path", default=str(STABLE_DIGEST_PATH))
    p_all.set_defaults(func=_cmd_run_all)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except InboxDigestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
