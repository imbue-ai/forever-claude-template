# cloudflare_tunnel

Background service that watches `runtime/secrets/cloudflare_tunnel.env` for a
Cloudflare tunnel token and runs `cloudflared` with it, exposing the agent's
services (terminal, web, etc.) over a Cloudflare tunnel. No-ops until a token
is provided, so local-only agents don't need Cloudflare configured; removing
the file stops `cloudflared`.

`runtime/secrets/` is a directory of per-secret `*.env` files -- each writer
(this token, `restic.env` for backups, `telegram.env` for the bot) owns its
own file, so they never clobber one another.
