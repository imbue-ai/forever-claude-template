"""Verify that ``.mngr/settings.toml`` create-templates compose as expected.

The minds/FCT setup runs ``mngr create --template system_services --template <mode>``
and relies on the fact that tuple-typed options (e.g. ``extra_provision_command``)
concatenate when multiple templates stack, while scalar-typed options (e.g.
``provider``) get overridden by the latter template.

If that behaviour ever regresses in vendor/mngr, the per-mode provisioning
on minds hosts silently loses either the shared ``system_services`` setup
(e.g. the default tmux config) or the mode-specific commands. These tests
pin the contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import tomlkit
from click.core import ParameterSource
from imbue.mngr.cli.common_opts import apply_create_template
from imbue.mngr.config.data_types import CreateTemplate, CreateTemplateName, MngrConfig

_REPO_ROOT = Path(__file__).parent
_SETTINGS_PATH = _REPO_ROOT / ".mngr" / "settings.toml"
_TMUX_MARKER = ".tmux.conf"


def _load_create_templates() -> dict[CreateTemplateName, CreateTemplate]:
    """Read .mngr/settings.toml and return its create_templates as CreateTemplate objects."""
    raw = tomlkit.parse(_SETTINGS_PATH.read_text()).unwrap()
    raw_templates: dict[str, dict[str, Any]] = raw.get("create_templates", {})
    return {
        CreateTemplateName(name): CreateTemplate.model_construct(options=dict(opts))
        for name, opts in raw_templates.items()
    }


def _make_ctx(params: dict[str, Any]) -> click.Context:
    ctx = click.Context(click.Command("create"))
    ctx.params = params
    for name in params:
        ctx.set_parameter_source(name, ParameterSource.DEFAULT)
    return ctx


def _apply(
    template_names: tuple[str, ...],
    *,
    extra_param_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    templates = _load_create_templates()
    config = MngrConfig(prefix="mngr-", create_templates=templates)
    params: dict[str, Any] = {
        "template": template_names,
        # tuple-typed CLI options that templates may set
        "extra_provision_command": (),
        "extra_window": (),
        "env": (),
        "pass_env": (),
        "pass_host_env": (),
        "build_arg": (),
        "start_arg": (),
        "setting": (),
        "agent_args": (),
        # scalar-typed CLI options that templates may set
        "type": None,
        "provider": None,
        "target_path": None,
        "worktree_base_folder": None,
        "idle_mode": None,
        "message": None,
        "name": "default",
    }
    params.update(extra_param_defaults or {})
    ctx = _make_ctx(params)
    return apply_create_template(ctx, ctx.params.copy(), config)


def test_system_services_template_writes_default_tmux_conf() -> None:
    """The shared `system_services` template provisions a default ~/.tmux.conf."""
    result = _apply(("system_services",))
    tmux_commands = [
        cmd for cmd in result["extra_provision_command"] if _TMUX_MARKER in cmd
    ]
    assert len(tmux_commands) == 1, (
        f"expected exactly one tmux-conf provisioning command from system_services, got {tmux_commands!r}"
    )
    only_command = tmux_commands[0]
    assert "set -g alternate-screen off" in only_command
    assert "set -g mouse on" in only_command


def test_system_services_extra_provision_command_stacks_with_lima() -> None:
    """`system_services` + `lima`: lima's provisioning commands all survive alongside system_services's."""
    result = _apply(("system_services", "lima"))
    commands = result["extra_provision_command"]
    assert any(_TMUX_MARKER in cmd for cmd in commands)
    # Spot-check several distinct lima provisioning commands to confirm the
    # entire list (not just the first entry) is concatenated.
    assert any("sudo mkdir -p /worktree" in cmd for cmd in commands)
    assert any("npm install -g latchkey" in cmd for cmd in commands)
    assert any("playwright install" in cmd for cmd in commands)


def test_system_services_extra_provision_command_present_for_docker_mode() -> None:
    """`system_services` + `docker`: docker has no `extra_provision_command` of its own, so only system_services's runs."""
    result = _apply(("system_services", "docker"))
    commands = result["extra_provision_command"]
    assert any(_TMUX_MARKER in cmd for cmd in commands)


def test_docker_template_adds_sys_ptrace_cap_via_start_arg() -> None:
    """`system_services` + `docker`: SYS_PTRACE cap is on `start_arg` (passed to `docker run`), not `build_arg`."""
    result = _apply(("system_services", "docker"))
    assert "--cap-add=SYS_PTRACE" in result["start_arg"]
    assert "--cap-add=SYS_PTRACE" not in result["build_arg"]


def test_scalar_template_options_override_rather_than_stack() -> None:
    """Scalar-typed options (e.g. provider) get overridden by the latter template."""
    result = _apply(("system_services", "docker"))
    assert result["provider"] == "docker"
    assert result["target_path"] == "/code/"
