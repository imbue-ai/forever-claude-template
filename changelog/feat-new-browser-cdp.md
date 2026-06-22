Infrastructure for the `browser` web service:

- `deferred_install.sh` installs Chromium (via Playwright) on first boot. No Xvfb, ffmpeg, or xdotool are needed: the browser runs headless and is streamed via CDP screencast.

- `supervisord.conf` runs the `browser` program (the streamed-browser service, registered at `/service/browser/`) headless — no virtual display.

- Root `pyproject.toml` adds the `browser` workspace member and an `openai>=2.20.0` override so browser-use and litellm resolve against a single lockfile.

Also synced the vendored `mngr` (`vendor/mngr`) to the latest upstream main.
