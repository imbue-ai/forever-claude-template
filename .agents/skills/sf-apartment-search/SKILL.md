---
name: sf-apartment-search
description: Research San Francisco apartment listings matching a user's hard criteria (budget, bed count, required amenities) and a walk-distance constraint from an anchor address. Scrapes Craigslist, rentalsinsf, Zumper, and PadMapper (Playwright with stealth for the anti-bot sites); also parses Equity Residential / Essex / AvalonBay building pages if the user drops their HTML into the output dir; explicitly lists Apartments.com, Zillow, and Trulia as blocked so the user can do a manual pass. Use when helping someone hunt for an SF apartment with specific price / layout / amenity requirements.
metadata:
  crystallized: true
---

# SF apartment search

## When to use

- User asks you to find SF apartment listings with hard constraints on
  budget, bed count, required amenities (in-unit laundry, dishwasher,
  etc.), and a walk-distance cap from a specific anchor address.
- User's anchor is anywhere in SF proper. The skill is SF-specific
  (neighborhood slugs + source list are tuned to the city).
- A re-run with different criteria (tighter budget, different anchor,
  refreshed listings) or for a different person is the canonical use
  case.

## Inputs to gather from the user

Before running the script, make sure you have:

- **Anchor address** — typically their office. Free-form string.
- **Budget ceiling** — monthly rent in USD.
- **Bed count** — minimum and maximum; note whether studios / junior
  1BRs are acceptable (common case: `1BR OR jr-1BR`).
- **Required amenities** — free-form list. Most common: `in-unit laundry`,
  `dishwasher`.
- **Soft-proximity targets** — optional secondary addresses that should
  score as bonus proximity (e.g. Golden Gate Park).
- **Max walk time to anchor** — default 30 min ≈ 1.5 miles.

Record these in memory as a user-type memory (helps future sessions).

## Steps

### 1. Pick neighborhoods within the walk radius

This is a **judgment step**. The `run.py` script scrapes one Zumper page
per neighborhood slug you pass, so your slug list directly shapes the
output. Pick 4-10 SF neighborhood slugs that plausibly lie within the
walk radius of the anchor.

Rule of thumb: 1 mile ≈ 20 min walking. 30 min ≈ 1.5 miles.

If you do not already know the anchor, use WebSearch to identify its
neighborhood, then fan out to adjacent neighborhoods.

**Canonical SF slugs** (Zumper path segments):
`hayes-valley`, `tenderloin`, `western-addition`, `lower-haight`,
`alamo-square`, `nopa`, `duboce-triangle`, `civic-center`,
`cathedral-hill`, `haight-ashbury`, `castro`, `lower-nob-hill`, `soma`,
`mission-bay`, `mission-district`, `noe-valley`, `pacific-heights`,
`japantown`, `fillmore`, `marina`, `russian-hill`, `north-beach`,
`chinatown`, `potrero-hill`, `dogpatch`, `bernal-heights`.

**Hayes Valley anchor (292 Ivy St and similar) example**:
`hayes-valley,tenderloin,western-addition,lower-haight,alamo-square,nopa,duboce-triangle,civic-center,cathedral-hill,lower-nob-hill`

### 2. Run the fetch + parse pipeline

```bash
uv run .agents/skills/sf-apartment-search/scripts/run.py \
  --anchor "292 Ivy St, San Francisco" \
  --neighborhoods hayes-valley,tenderloin,western-addition,lower-haight,alamo-square,nopa,duboce-triangle,civic-center,cathedral-hill,lower-nob-hill \
  --budget 4200 \
  --allow-studio \
  --require-amenity "in-unit laundry" \
  --require-amenity "dishwasher" \
  --craigslist-postal 94102 \
  --output-dir /tmp/sf-apt-search
```

The script writes HTML dumps and `results.json` into `--output-dir`.

**Playwright browser must be installed.** If `uv run` complains about a
missing Chromium, run `uv run --with playwright python -m playwright install chromium` once, then rerun.

**Re-parsing without re-fetching**: pass `--skip-fetch` to reuse the HTML
already in the output directory. Useful when tuning amenity keywords or
the price floor.

**Optional: Equity Residential / Essex / AvalonBay building pages.**
Those sites are not fetched automatically (each has many building URLs
and no unified search). If the user wants them covered, manually save
building pages as `equity_<slug>.html`, `essex_<slug>.html`, or
`avalon_<slug>.html` under `--output-dir` and the parse step will pick
them up.

### 3. Inspect `results.json`

Structure:
- `good`: listings meeting every hard criterion, sorted by `price_min`.
- `maybe`: matched price + bed count, but one or more required
  amenities could not be confirmed from the scraped card text.
- `suspicious_cheap`: listings below `--min-price-floor` (default
  $1500), which on SF Craigslist are near-universally scams or
  room-shares misfiled as 1BR.
- `blocked_sources`: list of `{source, reason, attempted}`. Always
  includes Apartments.com / Zillow / Trulia (known hard-blocked).
- `stats`: raw listing counts per source.

### 4. Rank the top picks

Judgment step. Read `good` and select the top ~10 listings balancing:

1. Hard criteria already satisfied (all of `good` qualifies).
2. Estimated walk time to the anchor. Rule of thumb: 1 mile ≈ 20 min
   walking, ≈ 5 min biking. For each listing's neighborhood, use your
   general knowledge of SF geography or WebSearch the address.
3. Soft-proximity targets (e.g. Golden Gate Park). Same rule of thumb.
4. Amenity completeness — promote listings with both amenities
   confirmed over ones with just one.
5. Price within budget — tie-break on lower price.

Surface the `maybe` tier too: many of these likely do have the missing
amenity, just not tagged on Zumper. Flag them as "worth checking
manually".

### 5. Produce a markdown table for the user

Columns: Address, Neighborhood, Beds, Price, In-unit W/D, Dishwasher,
~Walk to anchor, ~Walk/bike to soft-proximity target, URL.

### 6. Surface caveats

Always include in the reply:

- **Blocked sources**: list the sources from `blocked_sources`
  (especially Apartments.com, Zillow, Trulia). Tell the user to do a
  manual browser pass if they want maximum coverage.
- **Walk times are estimates** — tell the user to verify top picks in
  Google Maps before committing.
- **Scam floor** — listings below `--min-price-floor` were excluded
  from the main output; surface the `suspicious_cheap` bucket only if
  the user asks.
- **Snapshot in time** — scrape is a point-in-time capture. Availability
  may have changed between scrape and viewing.

## Playwright stealth knobs (already baked into run.py)

These settings matter for getting through Cloudflare / Zumper's lighter
bot checks. They are documented here for future maintenance:

- Launch arg: `--disable-blink-features=AutomationControlled`.
- Desktop Chrome user agent (current version string).
- Context viewport 1440x900, locale `en-US`, `Accept-Language: en-US,en;q=0.9`.
- Init script removes `navigator.webdriver`, fakes `navigator.plugins`
  (non-empty array), sets `navigator.languages`.
- After `goto`, poll `page.title()` for up to 8 × 2.5s, breaking when
  the title stops containing "Just a moment", "Checking your browser",
  "Access", or "denied".

These defeat Cloudflare interstitials on Zumper/Equity/Essex. They do
**not** defeat Apartments.com (Akamai), Zillow (PerimeterX), or Trulia
(shared PerimeterX with Zillow) — those need a residential-proxy tool,
and are recorded as blocked so the user knows to do a manual pass.

## Possible future extensions

- Wire in Google Maps Distance Matrix API for precise walk times
  (requires an API key; worth it if this skill is used often).
- Generalize to non-SF cities (requires researching per-city source
  lists and neighborhood slugs).
- Add automatic fetching of Equity / Essex / AvalonBay building pages
  (currently the parser only runs against HTML the user drops in
  manually, since those sites have no unified search URL).
