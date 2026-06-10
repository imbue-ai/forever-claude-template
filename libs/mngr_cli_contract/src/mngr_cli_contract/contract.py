"""Validate that a ``mngr <subcommand> ...`` argv is accepted by the *live* mngr CLI.

Repo code shells out to the ``mngr`` CLI by constructing argvs. A test that
pins such an argv against a *hand-written expected argv* (via a stubbed
subprocess runner) only confirms "the code emits the bytes we told it to
emit" -- the expected argv is authored from the same assumption as the
production code, so the two drift together and the test can never notice when
vendor/mngr renames or removes the subcommand or one of its flags. That
divergence then surfaces only at runtime.

``assert_mngr_argv_valid`` closes that gap by resolving the argv against the
actual ``imbue.mngr.main.cli`` click command tree. It checks *shape* only --
the subcommand must exist and every option token must be recognized -- using
click's low-level ``OptionParser`` so value validators (``Path(exists=True)``,
callbacks, type coercion, required-option enforcement) do NOT run. We are
verifying the CLI surface the repo depends on, not the runtime values a
particular invocation carries.

This lives in its own workspace package so both repo-side pytest passes (the
root pass and the isolated apps/system_interface pass, which share one
workspace venv) import a single copy rather than duplicating the validator.
"""

from __future__ import annotations

from collections.abc import Sequence

import click
from imbue.mngr.main import cli


class MngrArgvContractError(AssertionError):
    """Raised when an argv is not accepted by the live mngr CLI surface."""


def assert_mngr_argv_valid(argv: Sequence[str]) -> None:
    """Assert that ``argv`` is structurally accepted by the live mngr CLI.

    ``argv`` is a full command line whose first element is the mngr binary
    (``"mngr"`` or an absolute path -- it is ignored, only ``argv[1:]`` is
    validated). Resolves the (possibly nested) subcommand against the live
    click tree and parses the remaining tokens with each command's low-level
    option parser.

    Raises ``MngrArgvContractError`` when the subcommand does not exist or any
    option token is unrecognized -- i.e. exactly the drift that a vendor/mngr
    CLI change would introduce. Does not raise on value-level problems
    (nonexistent paths, missing required options): those are not CLI-surface
    drift and would make the contract check brittle.
    """
    try:
        _resolve_against_cli(cli, click.Context(cli, info_name="mngr"), list(argv[1:]))
    except click.exceptions.ClickException as exc:
        raise MngrArgvContractError(
            f"mngr argv not accepted by the live CLI: {list(argv)!r}\n"
            f"  {type(exc).__name__}: {exc.format_message()}"
        ) from exc


def _resolve_against_cli(
    command: click.Command, ctx: click.Context, tokens: list[str]
) -> None:
    """Descend the click tree for ``tokens``, raising on an unknown subcommand
    or option. Recurses through nested groups (mngr's tree is shallow); a leaf
    command's low-level parser recognizes/rejects option tokens and handles
    arity without running click's value converters (which would, e.g., reject a
    not-yet-created file)."""
    if isinstance(command, click.Group):
        name, subcommand, rest = command.resolve_command(ctx, tokens)
        if subcommand is None:
            raise click.exceptions.UsageError(f"No such command {name!r}.")
        _resolve_against_cli(
            subcommand, click.Context(subcommand, info_name=name, parent=ctx), rest
        )
    else:
        command.make_parser(ctx).parse_args(args=list(tokens))
