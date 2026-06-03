"""Central contract: repo-root mngr argv builders vs the live mngr CLI.

This is the consolidated home (option 4b from the audit) for confronting the
``mngr ...`` argvs that root-importable code shells out to with the *live*
``imbue.mngr.main.cli`` command tree. It imports each touchpoint's pure
argv-builder and asserts the result is structurally accepted by mngr, so a
vendor/mngr subcommand or flag rename fails here at merge time -- the class of
regression that slipped through on PR #77.

Coverage split:
  - launch-task's create_worker emits argv via a Runner, so it is validated in
    its own ``create_worker_test.py`` from the captured calls.
  - apps/system_interface runs as an isolated package (own venv + pytest pass),
    so its builders are validated in
    ``imbue/system_interface/mngr_cli_argv_contract_test.py``.
  - The remaining root-importable touchpoints (bootstrap, telegram_bot) are
    validated here.
"""

from __future__ import annotations

from bootstrap.manager import _build_create_chat_command
from telegram_bot.bot import _build_message_command

from mngr_cli_contract import assert_mngr_argv_valid


def test_bootstrap_initial_chat_create_argv_accepted_by_live_cli() -> None:
    # A ``workspace`` label is supplied so the builder's label-resolution
    # short-circuits without reading host files; both label branches are
    # exercised (workspace + project).
    argv = _build_create_chat_command(
        host_name="host-1",
        labels={"workspace": "ws", "project": "proj"},
    )
    assert_mngr_argv_valid(argv)


def test_bootstrap_initial_chat_create_argv_minimal_labels() -> None:
    argv = _build_create_chat_command(host_name="host-1", labels={"workspace": "ws"})
    assert_mngr_argv_valid(argv)


def test_telegram_message_argv_accepted_by_live_cli() -> None:
    argv = _build_message_command(agent_name="demo", message="hello from telegram")
    assert_mngr_argv_valid(argv)
