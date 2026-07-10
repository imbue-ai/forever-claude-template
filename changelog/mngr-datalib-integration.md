- Integrated [datalib](https://github.com/imbue-ai/datalib) (the `frankweiler`
  tools) as the default way agents retrieve, search, and import the user's own
  personal data -- past chat conversations, Slack, email, GitHub, Notion, and
  contacts -- into a local searchable mirror.

- Added a `datalib` skill (`.agents/skills/datalib/SKILL.md`) that teaches the
  agent to search the local store over its HTTP API (or the qmd index) and to
  import / refresh sources with `frankweiler-sync`. Web-API sources authenticate
  through the existing latchkey gateway, reusing the same permission flow.

- `scripts/setup_system.sh` installs the fully-static musl `frankweiler-sync` /
  `frankweiler-http` binaries into the image (pinned to `v0.16.0`), and
  `.mngr/settings.toml` sets `FRANKWEILER_ROOT=/mngr/datalib` -- a persistent,
  non-git-backed data root on the `/mngr` volume.

- Steered agents toward the skill from `CLAUDE.md` for questions about the
  user's own history.

- MVP scope: Slack, GitHub, Notion, and email (Google Takeout `.mbox`) work
  through the Minds latchkey gateway. Cloudflare-walled web sources (claude.ai,
  ChatGPT) are not reliable inside Minds because latchkey routes through its
  gateway and bypasses datalib's Chrome-impersonating curl shim.
