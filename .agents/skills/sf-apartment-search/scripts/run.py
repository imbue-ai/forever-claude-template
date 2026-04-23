# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright>=1.40",
#   "beautifulsoup4>=4.12",
#   "requests>=2.31",
# ]
# ///
"""Fetch + parse + filter SF apartment listings.

Fetches HTML from Craigslist + rentalsinsf (plain HTTP) and Zumper +
PadMapper (Playwright with stealth). Of those four, only Craigslist and
Zumper are parsed into the ranked `good` / `maybe` buckets in
results.json; `rentalsinsf.html` and `padmapper_sf_1br.html` are written
to the output dir for manual review only (no parser wired up). Equity
Residential, Essex, and AvalonBay are not fetched automatically, but if
the caller drops `equity_*.html` / `essex_*.html` / `avalon_*.html`
files into the output dir, they are parsed alongside the Craigslist and
Zumper results. Records Apartments.com / Zillow / Trulia as
attempted-but-blocked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
"""

# Sources known to block even with stealth. Recorded for the user, not fetched.
KNOWN_BLOCKED = [
    ("apartments.com", "Akamai bot detection returns Access Denied even with stealth"),
    ("zillow.com", "PerimeterX bot wall blocks headless browsers"),
    ("trulia.com", "Same PerimeterX wall as Zillow (shared owner)"),
]


@dataclass
class Listing:
    source: str
    address: str
    price_text: str
    beds_text: str
    amenities_text: str
    url: str
    raw_excerpt: str
    neighborhood: str = ""
    price_min: int | None = None
    price_max: int | None = None
    in_unit_laundry: bool = False
    dishwasher: bool = False


@dataclass
class Result:
    good: list[dict] = field(default_factory=list)
    maybe: list[dict] = field(default_factory=list)
    suspicious_cheap: list[dict] = field(default_factory=list)
    blocked_sources: list[dict] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------- fetch

def fetch_craigslist(
    budget: int,
    min_beds: int,
    max_beds: int,
    postal: str,
    radius_mi: float,
    out_dir: Path,
) -> dict[str, str]:
    """Craigslist search is WebFetch-friendly (no anti-bot).

    Returns a fetch_report entry: always includes 'path'; includes 'error'
    if the fetch failed so collect_blocked() can surface the failure.
    """
    params = {
        "max_price": str(budget),
        "min_bedrooms": str(min_beds),
        "max_bedrooms": str(max_beds),
        "laundry": "1",
        "search_distance": str(radius_mi),
        "postal": postal,
        "hasPic": "1",
    }
    url = (
        "https://sfbay.craigslist.org/search/sfc/apa?"
        + urllib.parse.urlencode(params)
    )
    path = out_dir / "craigslist.html"
    entry: dict[str, str] = {"path": str(path)}
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        path.write_text(resp.text, encoding="utf-8")
    except requests.RequestException as exc:
        path.write_text(f"<!-- fetch error: {exc} -->", encoding="utf-8")
        entry["error"] = str(exc)
    return entry


def fetch_rentalsinsf(out_dir: Path) -> dict[str, str]:
    """Fetch rentalsinsf listings page.

    Returns a fetch_report entry: always includes 'path'; includes 'error'
    if the fetch failed so collect_blocked() can surface the failure.
    """
    url = "https://www.rentalsinsf.com/listings"
    path = out_dir / "rentalsinsf.html"
    entry: dict[str, str] = {"path": str(path)}
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        path.write_text(resp.text, encoding="utf-8")
    except requests.RequestException as exc:
        path.write_text(f"<!-- fetch error: {exc} -->", encoding="utf-8")
        entry["error"] = str(exc)
    return entry


def fetch_with_playwright(targets: dict[str, str], out_dir: Path) -> dict[str, dict]:
    """Fetch multiple URLs through a stealth-configured Playwright browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            name: {"url": url, "error": "playwright not installed"}
            for name, url in targets.items()
        }

    report: dict[str, dict] = {}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # noqa: BLE001 - browser launch can fail many ways
            return {
                name: {"url": url, "error": f"browser launch failed: {exc}"}
                for name, url in targets.items()
            }
        try:
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                },
            )
            ctx.add_init_script(STEALTH_INIT_SCRIPT)
            for name, url in targets.items():
                entry: dict[str, str | int] = {"url": url}
                page = None
                try:
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    for _ in range(8):
                        page.wait_for_timeout(2500)
                        title = page.title()
                        tl = title.lower()
                        if (
                            "just a moment" not in tl
                            and "checking your browser" not in tl
                            and "access" not in tl
                            and "denied" not in tl
                        ):
                            break
                    html = page.content()
                    title = page.title()
                    (out_dir / f"{name}.html").write_text(html, encoding="utf-8")
                    entry["title"] = title
                    entry["length"] = len(html)
                    # detect hard block in content
                    if "Access Denied" in html or "access to this page has been denied" in html.lower():
                        entry["blocked"] = "bot detection returned Access Denied"
                except Exception as exc:  # noqa: BLE001 - navigation errors vary
                    entry["error"] = str(exc)
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:  # noqa: BLE001 - best-effort page cleanup
                            pass
                report[name] = entry
        finally:
            browser.close()
    return report


# --------------------------------------------------------------------- parse

PRICE_RE = re.compile(r"\$(\d[\d,]*)")


def parse_price_range(text: str) -> tuple[int, int] | None:
    nums = [int(n.replace(",", "")) for n in PRICE_RE.findall(text)]
    nums = [n for n in nums if 500 <= n <= 20000]
    if not nums:
        return None
    return min(nums), max(nums)


_JR_TOKEN_RE = re.compile(r"\b(?:jr|junior)\b")


def extract_bed_count(text: str) -> tuple[int, bool]:
    """Return (min_bed_count, is_studio). Studio counts as 0."""
    t = text.lower()
    is_studio = "studio" in t or bool(_JR_TOKEN_RE.search(t))
    m = re.search(r"(\d+)\s*(?:bed|br|bd)", t)
    if m:
        return int(m.group(1)), is_studio
    if is_studio:
        return 0, True
    return 99, False


def unit_matches_bed_criteria(
    bed_text: str,
    min_beds: int,
    max_beds: int,
    allow_studio: bool,
) -> bool:
    bc, is_studio = extract_bed_count(bed_text)
    if allow_studio and is_studio:
        return True
    return min_beds <= bc <= max_beds


def has_amenity(amenities: str, text: str, keyword: str) -> bool:
    combined = (amenities + " " + text).lower()
    k = keyword.lower()
    if k == "in-unit laundry":
        return any(
            m in combined
            for m in ("in-unit laundry", "in unit laundry", "w/d in unit", "washer/dryer in", "washer / dryer in")
        )
    if k == "dishwasher":
        return "dishwasher" in combined
    return k in combined


def parse_zumper(html: str, neighborhood: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    for row in soup.find_all(class_=re.compile(r"ListingCard", re.I)):
        if not hasattr(row, "find"):
            continue
        addr_el = row.find(class_=re.compile(r"address", re.I))
        price_el = row.find(class_=re.compile(r"price", re.I))
        beds_el = row.find(class_=re.compile(r"beds", re.I))
        amen_el = row.find(class_=re.compile(r"amenit", re.I))
        link_el = row.find("a", href=True)
        addr = addr_el.get_text(" ", strip=True) if addr_el else ""
        price = price_el.get_text(" ", strip=True) if price_el else ""
        beds = beds_el.get_text(" ", strip=True) if beds_el else ""
        amen = amen_el.get_text(" ", strip=True) if amen_el else ""
        text = row.get_text(" ", strip=True)[:400]
        if not (addr and price):
            continue
        link = link_el["href"] if link_el else ""
        url = f"https://www.zumper.com{link}" if link.startswith("/") else link
        out.append(
            Listing(
                source="Zumper",
                address=addr,
                price_text=price,
                beds_text=beds,
                amenities_text=amen,
                url=url,
                raw_excerpt=text,
                neighborhood=neighborhood,
            )
        )
    return out


CL_ROW_SELECTOR = re.compile(r"cl-static-search-result|cl-search-result", re.I)


def parse_craigslist(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    for row in soup.find_all(class_=CL_ROW_SELECTOR):
        if not hasattr(row, "find"):
            continue
        title_el = row.find(class_=re.compile(r"title", re.I))
        price_el = row.find(class_=re.compile(r"price", re.I))
        link_el = row.find("a", href=True)
        meta_el = row.find(class_=re.compile(r"meta|housing|attr", re.I))
        title = title_el.get_text(" ", strip=True) if title_el else ""
        price = price_el.get_text(" ", strip=True) if price_el else ""
        meta = meta_el.get_text(" ", strip=True) if meta_el else ""
        text = row.get_text(" ", strip=True)[:400]
        if not (title and price):
            continue
        link = link_el["href"] if link_el else ""
        out.append(
            Listing(
                source="Craigslist",
                address=title,
                price_text=price,
                beds_text=meta,
                amenities_text=meta,
                url=link,
                raw_excerpt=text,
                neighborhood="",
            )
        )
    return out


def parse_equity_like(html: str, building_slug: str, source_name: str) -> list[Listing]:
    """Equity/Essex/AvalonBay building pages list floorplan-level price boxes."""
    out: list[Listing] = []
    # Strip tags, collapse whitespace, then scan around each price mention.
    flat = re.sub(r"<[^>]+>", " ", html)
    flat = re.sub(r"\s+", " ", flat)
    # Each floorplan box tends to have "Studio" or "X Bed" within ~200 chars of
    # its price. We grab those pairs as pseudo-listings.
    seen: set[tuple[str, str]] = set()
    for m in PRICE_RE.finditer(flat):
        price = m.group(1).replace(",", "")
        if not price.isdigit():
            continue
        p = int(price)
        # Noise band: filter out obvious non-prices (square footage under
        # 1500, unit/building id numbers above 20000). Semantic filtering
        # against the user's --budget and --min-price-floor happens in
        # classify(); keep this band wide enough not to pre-empt it.
        if not (1500 <= p <= 20000):
            continue
        start = max(0, m.start() - 180)
        end = min(len(flat), m.end() + 60)
        ctx = flat[start:end].strip()
        bed_match = re.search(
            r"(studio|1\s*bed|2\s*bed|3\s*bed|1br|jr\s*1br|1x1)", ctx, re.I
        )
        if not bed_match:
            continue
        key = (price, bed_match.group(1).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Listing(
                source=source_name,
                address=building_slug.replace("_", " ").title(),
                price_text=f"${price}",
                beds_text=bed_match.group(1),
                amenities_text=ctx,
                url="",
                raw_excerpt=ctx[-200:],
                neighborhood="",
            )
        )
    return out


# --------------------------------------------------------------------- filter

def classify(
    listings: Iterable[Listing],
    budget: int,
    min_beds: int,
    max_beds: int,
    allow_studio: bool,
    required_amenities: list[str],
    min_price_floor: int,
) -> Result:
    result = Result()
    source_counts: dict[str, int] = {}
    for l in listings:
        source_counts[l.source] = source_counts.get(l.source, 0) + 1
        pr = parse_price_range(l.price_text)
        if not pr:
            continue
        l.price_min, l.price_max = pr
        l.in_unit_laundry = has_amenity(l.amenities_text, l.raw_excerpt, "in-unit laundry")
        l.dishwasher = has_amenity(l.amenities_text, l.raw_excerpt, "dishwasher")
        entry = asdict(l)
        if l.price_min < min_price_floor:
            result.suspicious_cheap.append(entry)
            continue
        if l.price_min > budget:
            continue
        if not unit_matches_bed_criteria(l.beds_text, min_beds, max_beds, allow_studio):
            continue
        all_amenities_met = True
        for a in required_amenities:
            if not has_amenity(l.amenities_text, l.raw_excerpt, a):
                all_amenities_met = False
                break
        if all_amenities_met:
            result.good.append(entry)
        else:
            result.maybe.append(entry)
    # dedupe each bucket by (address, price_text)
    for bucket in ("good", "maybe", "suspicious_cheap"):
        seen: set[tuple[str, str]] = set()
        uniq: list[dict] = []
        for e in getattr(result, bucket):
            key = (e["address"].strip().lower(), e["price_text"].strip())
            if key in seen:
                continue
            seen.add(key)
            uniq.append(e)
        uniq.sort(key=lambda e: e["price_min"] or 0)
        setattr(result, bucket, uniq)
    result.stats = source_counts
    return result


# --------------------------------------------------------------------- main

def build_playwright_targets(neighborhoods: list[str], budget: int) -> dict[str, str]:
    targets: dict[str, str] = {}
    for nb in neighborhoods:
        slug = nb.strip().lower()
        if not slug:
            continue
        targets[f"zumper_{slug}"] = (
            f"https://www.zumper.com/apartments-for-rent/san-francisco-ca/{slug}"
        )
    targets["padmapper_sf_1br"] = (
        "https://www.padmapper.com/apartments/san-francisco-ca/"
        f"1-bedroom-apartments-under-{budget}-price"
    )
    return targets


def run_fetch(args: argparse.Namespace, out_dir: Path) -> dict[str, dict]:
    report: dict[str, dict] = {}
    neighborhoods = [n for n in args.neighborhoods.split(",") if n.strip()]
    report["craigslist"] = fetch_craigslist(
        budget=args.budget,
        min_beds=args.min_beds,
        max_beds=args.max_beds,
        postal=args.craigslist_postal,
        radius_mi=args.max_walk_miles,
        out_dir=out_dir,
    )
    report["rentalsinsf"] = fetch_rentalsinsf(out_dir)
    playwright_targets = build_playwright_targets(neighborhoods, args.budget)
    report.update(fetch_with_playwright(playwright_targets, out_dir))
    return report


def parse_saved(out_dir: Path, neighborhoods: list[str]) -> list[Listing]:
    listings: list[Listing] = []
    cl_path = out_dir / "craigslist.html"
    if cl_path.exists():
        listings.extend(parse_craigslist(cl_path.read_text(encoding="utf-8", errors="ignore")))
    for nb in neighborhoods:
        slug = nb.strip().lower()
        if not slug:
            continue
        p = out_dir / f"zumper_{slug}.html"
        if p.exists():
            listings.extend(
                parse_zumper(p.read_text(encoding="utf-8", errors="ignore"), slug)
            )
    # Equity/Essex/AvalonBay: parse any building-level html stashed by name.
    for p in out_dir.glob("equity_*.html"):
        slug = p.stem.removeprefix("equity_")
        listings.extend(parse_equity_like(p.read_text(encoding="utf-8", errors="ignore"), slug, "Equity"))
    for p in out_dir.glob("essex_*.html"):
        slug = p.stem.removeprefix("essex_")
        listings.extend(parse_equity_like(p.read_text(encoding="utf-8", errors="ignore"), slug, "Essex"))
    for p in out_dir.glob("avalon_*.html"):
        slug = p.stem.removeprefix("avalon_")
        listings.extend(parse_equity_like(p.read_text(encoding="utf-8", errors="ignore"), slug, "AvalonBay"))
    return listings


def collect_blocked(fetch_report: dict[str, dict]) -> list[dict]:
    blocked: list[dict] = []
    for name, reason in KNOWN_BLOCKED:
        blocked.append({"source": name, "reason": reason, "attempted": False})
    for name, info in fetch_report.items():
        if "error" in info:
            blocked.append({"source": name, "reason": info["error"], "attempted": True})
        elif "blocked" in info:
            blocked.append({"source": name, "reason": info["blocked"], "attempted": True})
    return blocked


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--anchor", default="", help="anchor address for output provenance")
    p.add_argument(
        "--neighborhoods",
        required=True,
        help="comma-separated SF neighborhood slugs (e.g. hayes-valley,tenderloin)",
    )
    p.add_argument("--budget", type=int, required=True, help="max monthly rent USD")
    p.add_argument("--min-beds", type=int, default=0)
    p.add_argument("--max-beds", type=int, default=1)
    p.add_argument(
        "--allow-studio",
        action="store_true",
        help="treat studios and junior 1BRs as bed-count matches",
    )
    p.add_argument(
        "--require-amenity",
        action="append",
        default=[],
        help="required amenity keyword (repeatable, e.g. 'in-unit laundry', 'dishwasher')",
    )
    p.add_argument("--min-price-floor", type=int, default=1500)
    p.add_argument("--max-walk-miles", type=float, default=1.5)
    p.add_argument(
        "--craigslist-postal",
        default="94102",
        help="postal code for Craigslist radius search (default 94102 = Hayes Valley)",
    )
    p.add_argument("--output-dir", required=True)
    p.add_argument(
        "--skip-fetch",
        action="store_true",
        help="reuse HTML files already in --output-dir instead of fetching",
    )
    args = p.parse_args(argv)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_fetch:
        fetch_report: dict[str, dict] = {}
    else:
        fetch_report = run_fetch(args, out_dir)
        (out_dir / "fetch_report.json").write_text(json.dumps(fetch_report, indent=2))

    neighborhoods = [n for n in args.neighborhoods.split(",") if n.strip()]
    listings = parse_saved(out_dir, neighborhoods)
    result = classify(
        listings,
        budget=args.budget,
        min_beds=args.min_beds,
        max_beds=args.max_beds,
        allow_studio=args.allow_studio,
        required_amenities=args.require_amenity,
        min_price_floor=args.min_price_floor,
    )
    result.blocked_sources = collect_blocked(fetch_report)

    payload = {
        "anchor": args.anchor,
        "budget": args.budget,
        "neighborhoods": neighborhoods,
        "required_amenities": args.require_amenity,
        **asdict(result),
    }
    (out_dir / "results.json").write_text(json.dumps(payload, indent=2))

    print(
        f"good={len(result.good)} maybe={len(result.maybe)} "
        f"suspicious={len(result.suspicious_cheap)} blocked={len(result.blocked_sources)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
