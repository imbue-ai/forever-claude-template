"""Fixture-based tests for the deterministic body parser in run.py.

These cover the parsing that only misbehaves on specific input shapes (mime
selection, html stripping, base64url padding) -- the AI digest step is exercised
live, not here.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_multipart_prefers_plain_text_verbatim() -> None:
    record = run.to_raw_record(_load("multipart_plain_and_html"))
    assert record["body_kind"] == "plain"
    # The plain part is returned verbatim, including its footnote table -- not
    # the html alternative.
    assert "A concise plain-text summary." in record["body"]
    assert "[6] https://example.com/top-story" in record["body"]
    assert "<b>" not in record["body"]
    # Headers and the gmail_url are projected.
    assert record["from"] == "TLDR AI <dan@tldrnewsletter.com>"
    assert record["subject"] == "Daily roundup"
    assert record["id"] == "fixturemultipart01"
    assert record["gmail_url"].endswith("#inbox/fixturemultipart01")


def test_html_only_is_stripped_to_text() -> None:
    record = run.to_raw_record(_load("html_only"))
    assert record["body_kind"] == "html"
    body = record["body"]
    # Tags are gone, text is kept, and style/script contents are dropped.
    assert "Headline" in body
    assert "First paragraph with a link." in body
    assert "Second block" in body
    assert "<" not in body and ">" not in body
    assert "color:blue" not in body
    assert "var x=1" not in body


def test_empty_body_yields_none_kind() -> None:
    record = run.to_raw_record(_load("empty_body"))
    assert record["body_kind"] == "none"
    assert record["body"] == ""
    # Metadata is still projected even with no readable body.
    assert record["subject"] == "No body"


def test_base64url_decode_handles_missing_padding() -> None:
    # "any carnal pleasur" encodes to a length that needs padding restored.
    import base64

    payload = "any carnal pleasur"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    assert run._decode_b64url(encoded) == payload


def test_strip_html_block_tags_become_breaks() -> None:
    text = run.strip_html("<p>one</p><p>two</p><div>three</div>")
    # Block tags separate content onto their own lines (blank lines between
    # paragraphs are fine); the text content is preserved in order.
    assert [line for line in text.splitlines() if line] == ["one", "two", "three"]
