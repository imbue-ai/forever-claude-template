Remove Telegram entirely from the template.

The `libs/telegram_bot/` package (bot, send CLI, history viewer) and the `send-telegram-message` / `read-telegram-history` skills are deleted, along with the `TELEGRAM_BOT_TOKEN` / `TELEGRAM_USER_NAME` pass-through env vars, the `telegram-bot` dependency / workspace member / source, and the Dockerfile copy. Workspaces communicate via the web UI (inbound) and the minds notifications API (outbound).

`send-user-message` now uses its inline fallback as the sole channel; its probe-and-dispatch contract is unchanged, so any future deployment can add a new outbound channel by documenting its probe in that skill. Assorted docs/docstrings (README, CLAUDE.md, runtime_backup, cloudflare_tunnel, system_interface, dealing-with-the-unexpected) no longer reference Telegram.
