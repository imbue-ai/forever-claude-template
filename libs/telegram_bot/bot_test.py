"""Tests for the telegram bot's agent-delivery path.

Covers the message *body* the agent receives (it carries the sender username,
chat id, and original text so the agent has the context it needs to reply) and
the ``mngr message`` argv, which is confronted with the live
``imbue.mngr.main.cli`` tree so a vendor/mngr rename of that subcommand/flag
fails here at merge time.
"""

from __future__ import annotations

from mngr_cli_contract.contract import assert_mngr_argv_valid
from telegram_bot.bot import _build_message_command, _format_agent_message


def test_format_agent_message_includes_sender_context() -> None:
    body = _format_agent_message(username="alice", text="deploy please", chat_id=42)
    assert body == "telegram message from @alice (chat_id=42): deploy please"


def test_message_argv_accepted_by_live_cli() -> None:
    argv = _build_message_command(agent_name="demo", message="hello from telegram")
    assert_mngr_argv_valid(argv)
