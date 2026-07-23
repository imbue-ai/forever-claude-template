The browser fleet's Chromium engine is now Fortress (a from-source, BSD-3,
stealth-patched Chromium fork) instead of Playwright's own managed Chromium.
`session.py` and the agents' direct Playwright calls both launch the same
Fortress binary at `/opt/fortress/tilion-fortress/tilion`, so the whole
workspace runs one engine. The fleet gates browser launches on the Fortress
deferred-install marker.
