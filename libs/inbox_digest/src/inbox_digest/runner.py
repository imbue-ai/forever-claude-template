"""Flat, type-aware digest of your unread Gmail inbox.

Services run from /mngr/code (the repo root). Conventions:

- Runtime state files (anything written and read across runs, e.g.
  cursors, caches, last-visit timestamps): use cwd-relative paths like
  ``Path("runtime/inbox-digest/...")``. Do NOT use ``Path(__file__)``-based
  paths for runtime state -- the bug to avoid is one process writing
  to ``/mngr/code/runtime/...`` while another reads from
  ``/mngr/code/libs/<pkg>/runtime/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.

The ``ROOT_PATH`` env var is read so FastAPI emits prefix-aware
absolute URLs (OpenAPI links, redirects) when this app is reached
through the system_interface proxy at ``/service/inbox-digest/``. The
services.toml command sets ``ROOT_PATH=/service/inbox-digest`` for that
case. Standalone ``uv run inbox-digest`` leaves it empty so the app serves
at ``/``.
"""

import base64
import html
import json
import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

ROOT_PATH = os.environ.get("ROOT_PATH", "")
DIGEST_PATH = Path("runtime/inbox-digest/digest.json")

app = FastAPI(title="inbox-digest", root_path=ROOT_PATH)

# Category presentation: display order (most time-sensitive first), label, and
# the accent that color-codes the left spine + the monospace tab label. The
# accent is the page's only color signal -- it encodes the email's type, which
# is the one structural fact the whole view is organized around.
CATEGORIES: dict[str, dict[str, str]] = {
    "action": {"label": "Action", "accent": "#B4472A"},
    "event": {"label": "Event", "accent": "#1F6FB2"},
    "github": {"label": "GitHub", "accent": "#5B4FBE"},
    "newsletter": {"label": "Newsletter", "accent": "#2F6F4F"},
    "networking": {"label": "Networking", "accent": "#B07C2B"},
    "receipt": {"label": "Receipt", "accent": "#7A7068"},
    "promotion": {"label": "Promotion", "accent": "#9AA0A6"},
}
CATEGORY_ORDER = list(CATEGORIES.keys())


def load_digest() -> list[dict]:
    if not DIGEST_PATH.exists():
        return []
    return json.loads(DIGEST_PATH.read_text())


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def link(href: str | None, text: str) -> str:
    if not href:
        return esc(text)
    return f'<a href="{esc(href)}" target="_blank" rel="noopener noreferrer">{esc(text)}</a>'


def render_item_body(rec: dict) -> str:
    """Category-specific body. Each shape surfaces the fields the user
    confirmed -- enough that opening the original email is unnecessary."""
    cat = rec["category"]

    if cat == "newsletter":
        head = esc(rec.get("digest_title") or rec.get("sender_name", ""))
        rows = []
        for it in rec.get("items", []):
            meta = esc(it.get("read", ""))
            title = link(it.get("link"), it.get("title", ""))
            summary = esc(it.get("summary", ""))
            rows.append(
                f'<li><div class="art-title">{title}'
                f'{f"<span class=meta>{meta}</span>" if meta else ""}</div>'
                f'<div class="art-summary">{summary}</div></li>'
            )
        satire = (
            '<span class="flag">satire — not real news</span>'
            if rec.get("is_satire")
            else ""
        )
        return f'<div class="src">{head}{satire}</div><ul class="articles">{"".join(rows)}</ul>'

    if cat == "github":
        jobs = "".join(f"<li>{esc(j)}</li>" for j in rec.get("failed_jobs", []))
        return (
            f'<div class="line"><span class="status-fail">{esc(rec.get("status", ""))}</span> '
            f'&middot; {esc(rec.get("repo", ""))} &middot; {esc(rec.get("workflow", ""))}</div>'
            f'<div class="pr">{esc(rec.get("pr_title", ""))}</div>'
            f'<div class="jobs-label">Failed jobs</div><ul class="jobs">{jobs}</ul>'
            f'<div class="actions">{link(rec.get("link"), "View run")}</div>'
        )

    if cat == "action":
        due = rec.get("due", "")
        due_chip = (
            f'<span class="due">{esc(due)}</span>' if due and due != "—" else ""
        )
        ln = rec.get("link")
        open_html = f'<div class="actions">{link(ln, "Open")}</div>' if ln else ""
        return (
            f'<div class="act-title">{esc(rec.get("action", ""))}{due_chip}</div>'
            f'<div class="act-details">{esc(rec.get("details", ""))}</div>'
            f'{open_html}'
        )

    if cat == "event":
        head = esc(rec.get("digest_title") or rec.get("sender_name", ""))
        rows = []
        for ev in rec.get("events", []):
            where = esc(ev.get("where", ""))
            when = esc(ev.get("when", ""))
            what = esc(ev.get("what", ""))
            extra = ""
            if ev.get("change"):
                extra += f'<span class="flag">{esc(ev["change"])}</span>'
            tix = ev.get("rsvp_link") or ev.get("link")
            tix_label = "RSVP" if ev.get("rsvp_link") else "Tickets"
            tix_html = f' &middot; {link(tix, tix_label)}' if tix else ""
            rows.append(
                f'<li><span class="ev-what">{what}</span>{extra}'
                f'<div class="ev-meta">{when}{f" &middot; {where}" if where else ""}{tix_html}</div></li>'
            )
        rsvp_note = (
            '<span class="flag">RSVP requested</span>' if rec.get("rsvp") else ""
        )
        return f'<div class="src">{head}{rsvp_note}</div><ul class="events">{"".join(rows)}</ul>'

    if cat == "receipt":
        return (
            f'<div class="src">{esc(rec.get("sender_name", ""))}'
            f'<span class="amount">{esc(rec.get("amount", ""))}</span></div>'
            f'<div class="act-details">{esc(rec.get("details", ""))}</div>'
            f'<div class="actions">{link(rec.get("link"), "View receipt")}</div>'
        )

    if cat == "networking":
        return (
            f'<div class="src">{esc(rec.get("sender_name", ""))}</div>'
            f'<div class="act-details">{esc(rec.get("details", ""))}</div>'
            f'<div class="actions">{link(rec.get("reply_link"), "Reply")}</div>'
        )

    if cat == "promotion":
        return (
            f'<span class="promo-src">{esc(rec.get("sender_name", ""))}</span>'
            f'<span class="promo-line">{esc(rec.get("one_liner", ""))}</span>'
        )

    return f'<div class="act-details">{esc(rec.get("subject", ""))}</div>'


def render_record(rec: dict, idx: int) -> str:
    cat = rec["category"]
    cfg = CATEGORIES.get(cat, {"label": cat, "accent": "#888"})
    accent = cfg["accent"]
    # "view raw" + "open in Gmail" live on every record -- the unobtrusive
    # bridge to the unprocessed original (preserve-and-surface).
    raw_btn = (
        f'<button class="raw-toggle" data-id="{esc(rec["id"])}" '
        f'aria-expanded="false">view raw</button>'
    )
    gmail = (
        link(rec.get("gmail_url"), "open in Gmail")
        if rec.get("gmail_url")
        else ""
    )
    return (
        f'<article class="item" style="--accent:{accent}">'
        f'<div class="spine"></div>'
        f'<div class="body">'
        f'<div class="tab"><span class="cat">{esc(cfg["label"])}</span>'
        f'<span class="tools">{raw_btn}{gmail}</span></div>'
        f'{render_item_body(rec)}'
        f'<div class="raw-slot" id="raw-{esc(rec["id"])}" hidden></div>'
        f'</div></article>'
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inbox Digest</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Newsreader:ital,opsz@0,6..72;1,6..72&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{
  --paper:#E8E6DE; --card:#F5F3ED; --ink:#1B2026; --muted:#6A6F76;
  --rule:#D4D1C7; --link:#3A5C8A;
}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{margin:0;background:var(--paper);color:var(--ink);
  font-family:"Space Grotesk",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.45}}
a{{color:var(--link);text-decoration:none;border-bottom:1px solid rgba(58,92,138,.3)}}
a:hover{{border-bottom-color:var(--link)}}
.wrap{{max-width:760px;margin:0 auto;padding:0 20px 96px}}

/* Masthead */
.mast{{padding:40px 0 22px;border-bottom:2px solid var(--ink);margin-bottom:8px;
  display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap}}
.mast h1{{font-size:34px;font-weight:700;letter-spacing:-.02em;margin:0;line-height:1}}
.mast .date{{font-family:"IBM Plex Mono",monospace;font-size:12px;
  color:var(--muted);text-transform:uppercase;letter-spacing:.08em}}
.tally{{font-family:"IBM Plex Mono",monospace;font-size:12.5px;color:var(--muted);
  padding:12px 0 24px;border-bottom:1px solid var(--rule);
  display:flex;flex-wrap:wrap;gap:4px 18px}}
.tally b{{color:var(--ink);font-weight:500}}
.tally a{{border:none;color:var(--muted)}}
.tally a:hover{{color:var(--ink)}}

/* Item: left spine encodes category by color */
.item{{display:flex;gap:0;margin-top:14px;background:var(--card);
  border:1px solid var(--rule);border-radius:2px;overflow:hidden}}
.spine{{flex:0 0 4px;background:var(--accent)}}
.body{{flex:1;min-width:0;padding:14px 18px 15px}}
.tab{{display:flex;justify-content:space-between;align-items:center;
  margin-bottom:8px;gap:10px}}
.cat{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;font-weight:500;
  text-transform:uppercase;letter-spacing:.14em;color:var(--accent)}}
.tools{{display:flex;gap:12px;align-items:center;opacity:0;transition:opacity .12s}}
.item:hover .tools,.item:focus-within .tools{{opacity:1}}
.tools a,.raw-toggle{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;
  color:var(--muted);border:none;background:none;cursor:pointer;padding:0;
  letter-spacing:.04em}}
.tools a:hover,.raw-toggle:hover{{color:var(--ink)}}

.src{{font-weight:700;font-size:15.5px;letter-spacing:-.01em;
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}}
.amount{{font-family:"IBM Plex Mono",monospace;font-weight:500;font-size:14px;
  color:var(--ink)}}
.flag{{font-family:"IBM Plex Mono",monospace;font-size:9.5px;font-weight:500;
  text-transform:uppercase;letter-spacing:.1em;color:#fff;background:var(--accent);
  padding:2px 6px;border-radius:2px;align-self:center}}

/* Newsletter articles -- serif so they read like prose */
.articles,.events,.jobs{{list-style:none;margin:10px 0 0;padding:0}}
.articles li{{padding:9px 0;border-top:1px solid var(--rule)}}
.art-title{{font-size:14.5px;font-weight:500;display:flex;
  justify-content:space-between;gap:12px;align-items:baseline}}
.art-title .meta{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;
  color:var(--muted);white-space:nowrap;flex:0 0 auto}}
.art-summary{{font-family:"Newsreader",Georgia,serif;font-size:15px;
  line-height:1.5;color:#33383F;margin-top:3px}}

/* Events */
.events li{{padding:7px 0;border-top:1px solid var(--rule)}}
.ev-what{{font-weight:500;font-size:14.5px}}
.ev-meta{{font-family:"IBM Plex Mono",monospace;font-size:11.5px;
  color:var(--muted);margin-top:2px}}

/* Action */
.act-title{{font-weight:700;font-size:15.5px;letter-spacing:-.01em;
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}}
.act-details{{font-family:"Newsreader",Georgia,serif;font-size:15px;
  line-height:1.5;color:#33383F;margin-top:4px}}
.due{{font-family:"IBM Plex Mono",monospace;font-size:10px;font-weight:500;
  text-transform:uppercase;letter-spacing:.08em;color:#fff;background:var(--accent);
  padding:2px 7px;border-radius:2px}}

/* GitHub */
.line{{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted)}}
.status-fail{{color:#B4472A;font-weight:500;text-transform:uppercase;
  letter-spacing:.06em}}
.pr{{font-weight:500;font-size:14.5px;margin-top:4px}}
.jobs-label{{font-family:"IBM Plex Mono",monospace;font-size:10px;
  text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-top:8px}}
.jobs li{{font-family:"IBM Plex Mono",monospace;font-size:12px;color:#33383F;
  padding:2px 0 2px 14px;position:relative}}
.jobs li::before{{content:"\\00d7";position:absolute;left:0;color:#B4472A}}
.actions{{margin-top:9px;font-family:"IBM Plex Mono",monospace;font-size:11.5px}}

/* Promotions -- collapsed, quiet, but still visible */
details.promos{{margin-top:28px;border-top:1px solid var(--rule);padding-top:14px}}
details.promos summary{{font-family:"IBM Plex Mono",monospace;font-size:11px;
  text-transform:uppercase;letter-spacing:.12em;color:var(--muted);cursor:pointer;
  list-style:none}}
details.promos summary::-webkit-details-marker{{display:none}}
details.promos summary::before{{content:"+ ";color:var(--muted)}}
details.promos[open] summary::before{{content:"\\2212 "}}
.promo{{display:flex;gap:12px;align-items:baseline;padding:7px 0;
  border-top:1px solid var(--rule);margin-top:8px}}
.promo:first-of-type{{border-top:none}}
.promo-src{{font-family:"IBM Plex Mono",monospace;font-size:11px;font-weight:500;
  color:var(--muted);flex:0 0 92px;text-transform:uppercase;letter-spacing:.04em}}
.promo-line{{font-size:13.5px;color:#33383F}}

/* Raw email */
.raw-slot{{margin-top:12px;border-top:1px solid var(--rule);padding-top:10px}}
.raw-slot iframe{{width:100%;height:340px;border:1px solid var(--rule);
  border-radius:2px;background:#fff}}
.raw-note{{font-family:"IBM Plex Mono",monospace;font-size:10px;color:var(--muted);
  margin-top:5px}}

.empty{{font-family:"Newsreader",serif;font-size:18px;color:var(--muted);
  padding:60px 0;text-align:center}}

@media (max-width:560px){{
  .mast h1{{font-size:27px}}
  .tools{{opacity:1}}
  .body{{padding:13px 14px}}
}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style>
</head>
<body>
<div class="wrap">
  <header class="mast">
    <h1>Inbox Digest</h1>
    <span class="date">Unread &middot; {date}</span>
  </header>
  <nav class="tally">{tally}</nav>
  {sections}
  {promos}
</div>
<script>
// Relative URL ("raw/<id>") resolves against the page's base href -- which is
// "/service/inbox-digest/" behind the proxy and "/" standalone -- so it points
// at this service in both cases without depending on a ROOT_PATH env var.
document.querySelectorAll('.raw-toggle').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const id = btn.dataset.id;
    const slot = document.getElementById('raw-' + id);
    const open = btn.getAttribute('aria-expanded') === 'true';
    if (open) {{
      slot.hidden = true; slot.innerHTML = '';
      btn.setAttribute('aria-expanded','false'); btn.textContent = 'view raw';
      return;
    }}
    btn.setAttribute('aria-expanded','true'); btn.textContent = 'hide raw';
    slot.hidden = false;
    slot.innerHTML = '<iframe sandbox referrerpolicy="no-referrer" src="'
      + 'raw/' + encodeURIComponent(id) + '"></iframe>'
      + '<div class="raw-note">original email &middot; scripts and remote images blocked</div>';
  }});
}});
</script>
</body>
</html>"""


def render_page() -> str:
    records = load_digest()
    by_cat: dict[str, list[dict]] = {c: [] for c in CATEGORY_ORDER}
    for rec in records:
        by_cat.setdefault(rec["category"], []).append(rec)

    # Tally line: total + per-category counts, each jumping to its section.
    tally_bits = [f'<span><b>{len(records)}</b> unread</span>']
    sections = []
    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat, [])
        if not items or cat == "promotion":
            continue
        cfg = CATEGORIES[cat]
        tally_bits.append(
            f'<a href="#sec-{cat}"><b>{len(items)}</b> {esc(cfg["label"].lower())}'
            f'{"s" if len(items) != 1 else ""}</a>'
        )
        body = "".join(render_record(r, i) for i, r in enumerate(items))
        sections.append(f'<section id="sec-{cat}">{body}</section>')

    # Records whose category is neither a known section nor "promotion" still
    # belong on the page -- drop nothing silently. render_record already falls
    # back to a neutral accent and the raw category as its label.
    known = set(CATEGORY_ORDER) | {"promotion"}
    other = [rec for rec in records if rec.get("category") not in known]
    if other:
        tally_bits.append(
            f'<a href="#sec-other"><b>{len(other)}</b> other</a>'
        )
        body = "".join(render_record(r, i) for i, r in enumerate(other))
        sections.append(f'<section id="sec-other">{body}</section>')

    promos = by_cat.get("promotion", [])
    promos_html = ""
    if promos:
        rows = "".join(f'<div class="promo">{render_item_body(p)}</div>' for p in promos)
        promos_html = (
            f'<details class="promos"><summary>Promotions ({len(promos)})</summary>'
            f"{rows}</details>"
        )

    if not records:
        return PAGE.format(
            date="",
            tally="",
            sections='<div class="empty">No unread mail. Inbox zero.</div>',
            promos="",
        )

    return PAGE.format(
        date=_today(),
        tally="".join(tally_bits),
        sections="".join(sections),
        promos=promos_html,
    )


def _today() -> str:
    # Render date from the data if present, else leave generic. Avoids
    # importing wall-clock just for a header.
    records = load_digest()
    for r in records:
        d = r.get("date", "")
        if d:
            # "Thu, 18 Jun 2026 ..." -> "Thu 18 Jun 2026"
            parts = d.replace(",", "").split()
            if len(parts) >= 4:
                return " ".join(parts[:4])
    return "today"


# ---------------------------------------------------------------------------
# Raw email view: fetch the original message on demand, render it faithfully
# but safely (sandboxed iframe + strict CSP that blocks scripts and remote
# resources like tracking pixels). Falls back to the stored body text.
# ---------------------------------------------------------------------------

RAW_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:;"


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "===" * ((4 - len(data) % 4) % 4))


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip == 0 and data.strip():
            self.out.append(data.strip())


def _fetch_raw_html(msg_id: str) -> str | None:
    """Fetch the original message and return its text/html body, if any."""
    try:
        proc = subprocess.run(
            [
                "latchkey",
                "curl",
                "-s",
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format=full",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        msg = json.loads(proc.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if "payload" not in msg:
        return None

    found: dict[str, str | None] = {"html": None, "plain": None}

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/html" and data and not found["html"]:
            found["html"] = _b64(data).decode("utf-8", "replace")
        elif mime == "text/plain" and data and not found["plain"]:
            found["plain"] = _b64(data).decode("utf-8", "replace")
        for child in part.get("parts", []) or []:
            walk(child)

    walk(msg["payload"])
    if found["html"]:
        return found["html"]
    if found["plain"]:
        return f"<pre style='white-space:pre-wrap;font:14px/1.5 Georgia,serif'>{html.escape(found['plain'])}</pre>"
    return None


def _stored_body_html(msg_id: str) -> str:
    for rec in load_digest():
        if rec.get("id") == msg_id and rec.get("raw_body"):
            return (
                "<pre style='white-space:pre-wrap;font:14px/1.5 Georgia,serif'>"
                f"{html.escape(rec['raw_body'])}</pre>"
            )
    return "<p style='font:14px Georgia,serif;color:#666'>Raw message unavailable.</p>"


@app.get("/raw/{msg_id}", response_class=HTMLResponse)
def raw_email(msg_id: str) -> HTMLResponse:
    body = _fetch_raw_html(msg_id) or _stored_body_html(msg_id)
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{margin:14px;color:#1B2026;font-family:Georgia,serif}"
        "img{max-width:100%}</style></head><body>" + body + "</body></html>"
    )
    return HTMLResponse(doc, headers={"Content-Security-Policy": RAW_CSP})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(render_page())


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8081)


if __name__ == "__main__":
    main()
