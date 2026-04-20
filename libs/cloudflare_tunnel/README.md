# cloudflare_tunnel

Background service that watches `runtime/secrets/` for a Cloudflare tunnel
token and runs `cloudflared` with it, exposing the agent's services
(terminal, web, etc.) over a Cloudflare tunnel. No-ops until a token is
provided, so local-only agents don't need Cloudflare configured.
