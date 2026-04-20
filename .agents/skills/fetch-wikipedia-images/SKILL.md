---
name: fetch-wikipedia-images
description: Download primary Wikipedia article images locally via the en.wikipedia.org/wiki/Special:FilePath redirect (which works when direct upload.wikimedia.org access is blocked or cross-origin image loading fails). Use when a web UI served from this container needs Wikipedia images -- produces local image files plus a JSON manifest mapping article titles to file paths.
metadata:
  crystallized: true
---

# Fetch Wikipedia images

Downloads the primary image (the one shown in the article's infobox) for one
or more Wikipedia articles. Images go to a local directory and a JSON
manifest maps each input title to its saved filename (or `null` if the
article has no pageimage).

## When to use this

- You are building a web UI that needs Wikipedia images but
  `upload.wikimedia.org` is blocked from your environment or the web-tab
  proxy blocks cross-origin image loading.
- You need a stable local copy of an article's lead image for offline or
  same-origin serving.

## Invocation

```
uv run .agents/skills/fetch-wikipedia-images/scripts/run.py \
  --output-dir DIR \
  [--width 640] \
  [--manifest PATH] \
  [--delay 0.5] \
  TITLE [TITLE ...]
```

Arguments:

- `TITLE ...` (positional, required): one or more Wikipedia article titles.
  Titles may contain spaces, commas, and Unicode -- quote them as needed
  for your shell.
- `--output-dir DIR` (required): directory to write images into (created if
  missing).
- `--width N` (default `640`): requested pixel width; passed as both
  `pithumbsize` (API) and `width` (Special:FilePath).
- `--manifest PATH` (default stdout): write the JSON manifest to this path
  instead of stdout.
- `--delay SECONDS` (default `0.5`): sleep between image downloads to avoid
  429 rate-limiting.

Exit code: 0 on success, non-zero only if the Wikipedia API query itself
fails. Per-title download failures are logged to stderr and recorded as
`null` in the manifest.

## Manifest format

```json
{
  "Golden Gate Bridge": "golden_gate_bridge.jpg",
  "Mount Tamalpais": "mount_tamalpais.jpg",
  "Asdfqwerty Nonexistent Article": null
}
```

Values are filenames relative to `--output-dir`, so the manifest is portable
if you move the directory.

## How it works

1. One batched `GET` to `en.wikipedia.org/w/api.php` with
   `action=query&prop=pageimages&titles=t1|t2|...&pithumbsize=W&format=json&redirects=1`.
   Titles are chunked into groups of 50 (API limit).
2. For each returned page, extract the `pageimage` filename.
3. For each `(title, filename)`: `GET
   https://en.wikipedia.org/wiki/Special:FilePath/{quoted_filename}?width=W`
   using `urllib.request` with a browser-like `User-Agent`. This endpoint
   301s to the CDN; because the first hop is `en.wikipedia.org`, it works
   in environments where `upload.wikimedia.org` is blocked.
4. Sleep `--delay` seconds between downloads.
5. Save each image as `{slug(title)}{ext}` in `--output-dir`. Write the
   manifest JSON.

## Serving the images

This skill only fetches files. To serve them from a FastAPI app in the same
process, do so yourself -- it is a one-liner and varies by app layout:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/images", StaticFiles(directory="path/to/output-dir"), name="images")
```
