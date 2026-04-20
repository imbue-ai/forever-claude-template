# app_watcher

Background service that watches `runtime/applications.toml` and writes
`server_registered` / `server_deregistered` events to
`$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl` so the minds desktop
client can discover which application ports an agent is exposing.

Uses inotify when available on Linux, and falls back to mtime polling
(10-second interval) otherwise.
