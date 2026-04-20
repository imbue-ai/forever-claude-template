#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Download primary Wikipedia article images via Special:FilePath."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_ENDPOINT = "https://en.wikipedia.org/w/api.php"
FILEPATH_ENDPOINT = "https://en.wikipedia.org/wiki/Special:FilePath"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "fetch-wikipedia-images/1.0"
)
API_TITLE_BATCH = 50


def slugify(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_").lower()
    return slug or "image"


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def query_pageimages(titles: list[str], width: int) -> dict[str, str | None]:
    """Return a map from the caller's original title to pageimage filename (or None)."""
    result: dict[str, str | None] = {title: None for title in titles}
    for start in range(0, len(titles), API_TITLE_BATCH):
        batch = titles[start : start + API_TITLE_BATCH]
        params = {
            "action": "query",
            "prop": "pageimages",
            "titles": "|".join(batch),
            "pithumbsize": str(width),
            "format": "json",
            "redirects": "1",
            "formatversion": "2",
        }
        url = f"{API_ENDPOINT}?{urllib.parse.urlencode(params)}"
        data = fetch_json(url)
        query = data.get("query", {})

        canonical_to_original: dict[str, str] = {title: title for title in batch}
        for entry in query.get("normalized", []):
            original = entry.get("from")
            canonical = entry.get("to")
            if original in canonical_to_original and canonical is not None:
                canonical_to_original[canonical] = canonical_to_original.pop(original)
        for entry in query.get("redirects", []):
            original = entry.get("from")
            canonical = entry.get("to")
            if original in canonical_to_original and canonical is not None:
                canonical_to_original[canonical] = canonical_to_original.pop(original)

        for page in query.get("pages", []):
            canonical_title = page.get("title")
            pageimage = page.get("pageimage")
            if canonical_title is None or pageimage is None:
                continue
            original = canonical_to_original.get(canonical_title, canonical_title)
            if original in result:
                result[original] = pageimage
    return result


def download_image(
    filename: str, width: int, destination: Path
) -> None:
    quoted = urllib.parse.quote(filename, safe="")
    url = f"{FILEPATH_ENDPOINT}/{quoted}?width={width}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())


def run(
    titles: list[str],
    output_dir: Path,
    width: int,
    manifest_path: Path | None,
    delay: float,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        title_to_filename = query_pageimages(titles, width)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"error: Wikipedia API query failed: {exc}", file=sys.stderr)
        return 1

    manifest: dict[str, str | None] = {}
    used_local_names: set[str] = set()
    items = list(title_to_filename.items())
    for index, (title, filename) in enumerate(items):
        if filename is None:
            print(f"warn: no pageimage for {title!r}", file=sys.stderr)
            manifest[title] = None
            continue

        extension = Path(filename).suffix or ".jpg"
        base_slug = slugify(title)
        extension_lower = extension.lower()
        local_name = f"{base_slug}{extension_lower}"
        collision_index = 2
        while local_name in used_local_names:
            local_name = f"{base_slug}_{collision_index}{extension_lower}"
            collision_index += 1
        used_local_names.add(local_name)
        destination = output_dir / local_name
        try:
            download_image(filename, width, destination)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(
                f"warn: download failed for {title!r} ({filename}): {exc}",
                file=sys.stderr,
            )
            manifest[title] = None
        else:
            manifest[title] = local_name

        if delay > 0 and index < len(items) - 1:
            time.sleep(delay)

    serialized = json.dumps(manifest, indent=2, ensure_ascii=False)
    if manifest_path is None:
        print(serialized)
    else:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(serialized + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download primary Wikipedia article images locally via "
            "Special:FilePath, producing a JSON manifest."
        ),
    )
    parser.add_argument(
        "titles",
        nargs="+",
        metavar="TITLE",
        help="Wikipedia article titles to fetch images for.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory to write downloaded images into (created if missing).",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=640,
        help="Desired image width in pixels (default: 640).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to write the JSON manifest (default: stdout).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to sleep between image downloads (default: 0.5).",
    )
    args = parser.parse_args()

    return run(
        titles=args.titles,
        output_dir=args.output_dir,
        width=args.width,
        manifest_path=args.manifest,
        delay=args.delay,
    )


if __name__ == "__main__":
    sys.exit(main())
