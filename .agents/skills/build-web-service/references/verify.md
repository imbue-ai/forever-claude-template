# Verifying a web service

Run both checks. `curl` confirms the proxy reaches your backend;
Playwright catches iframe-rendering bugs that `curl` misses (the
"duplicated dockview tab bar" symptom is one).

## Step 1: curl through the system_interface

```bash
curl -sf http://127.0.0.1:8000/service/<name>/ -o /dev/null -w "%{http_code}\n"
```

Port 8000 is the system_interface; it proxies `/service/<name>/...`
to the URL registered in `runtime/applications.toml`. Expected: `200`.

Common failures:

- **502** -- backend not reachable. Either the app crashed (check
  `supervisorctl status <name>` and `/var/log/supervisor/<name>-stderr.log`)
  or it's bound to the wrong host. See cross-flow-gotchas.md.
- **404 from system_interface** -- the service name is not in
  `runtime/applications.toml`. Either `forward_port.py` was not run,
  or it was passed the wrong `--name`.
- **200 but the rendered page is the agent chat with a duplicated
  dockview tab bar** -- the system_interface could not reach your
  backend and is falling back to the top-level UI. See
  cross-flow-gotchas.md.

## Step 2: Playwright assertion

`curl -I` alone does not catch iframe-rendering bugs. Use Playwright
(preinstalled in the root venv per `CLAUDE.md`):

```python
# /tmp/verify_<name>.py
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://127.0.0.1:8000/service/<name>/", wait_until="networkidle")
    title = page.title()
    body = page.content()
    print("title:", title)
    print("body len:", len(body))
    assert "<your-expected-marker>" in body, body[:500]
    browser.close()
```

Run with `uv run python /tmp/verify_<name>.py`.

Pick a marker that **only** appears when your app rendered correctly
-- a heading, a data-driven element. Do not assert on `<html>` or
`<body>`; those appear in error pages too.
