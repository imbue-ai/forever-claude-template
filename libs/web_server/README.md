# web_server

Minimal example Flask web server shipped with the template.

Exposes one page at `/` with placeholder copy so a newly created project has
something non-empty in the desktop client's `web` application slot. Edit
`src/web_server/runner.py` to replace the placeholder with real routes, or
swap it out in `supervisord.conf` (the `web` program) for any other web app.
