# Testing a web frontend

Shared testing guidance for any artifact that serves a web UI -- a scaffolded
service (`artifact-service.md`) or the system interface
(`artifact-system-interface.md`). Apply it alongside the universal contract in
`harden-artifact.md`; your artifact reference adds the specifics (where the app
lives, its stack, its test entry points).

## Drive an isolated instance, never the live one

Exercise the app **in-process** or against a **throwaway** instance on an
alternate port -- never `:8000` (the system_interface proxy) and never the live
port. Drive the Flask app with its test client (`app.test_client()`), or launch a
disposable threaded Werkzeug server (`run_simple(..., threaded=True)`). Never
restart, curl, or "reveal" the live service; revealing the change is the lead's
job after merge.

## Assert on real behavior

Assert on markers that are true if and only if a route behaves correctly --
status, the rendered content, the raw-data/source affordance, and the empty and
overflow states -- not just that the route returned `200`. Add Playwright
coverage wherever the value is in the rendered UI rather than the JSON, driving
it against the isolated instance.

## Look at the rendered page

**If your artifact renders a frontend, you MUST look at the actual rendered page
-- not just assert on the DOM.** A clean build and passing Playwright assertions
prove the markup and wiring exist; they do NOT prove the page *looks* right --
layout, spacing, alignment, overflow/truncation, color/contrast, z-order, and
whether your change broke something visually elsewhere. Before you report `done`,
capture screenshots of every page and state your change affects (driving the same
isolated Playwright instance; `page.screenshot(...)`, and
`page.set_viewport_size(...)` if layout is width-sensitive), then **actually open
and view those images and judge them with your own eyes.** Fix and re-screenshot
until correct. These development screenshots are a manual check, not a committed
test.
