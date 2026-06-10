"""Verify that ``.mngr/settings.toml`` create-templates compose as expected.

The minds/FCT setup runs ``mngr create --template main --template <mode>``
and relies on the fact that tuple-typed options (e.g. ``extra_provision_command``)
concatenate when multiple templates stack, while scalar-typed options (e.g.
``provider``) get overridden by the latter template.

If that behaviour ever regresses in vendor/mngr, the per-mode provisioning
on minds hosts silently loses either the shared ``main`` setup (e.g. the
default tmux config) or the mode-specific commands. These tests pin the
contract.
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


def test_main_template_writes_default_tmux_conf() -> None:
    """The shared `main` template provisions a default ~/.tmux.conf."""
    result = _apply(("main",))
    tmux_commands = [
        cmd for cmd in result["extra_provision_command"] if _TMUX_MARKER in cmd
    ]
    assert len(tmux_commands) == 1, (
        f"expected exactly one tmux-conf provisioning command from main, got {tmux_commands!r}"
    )
    only_command = tmux_commands[0]
    assert "set -g alternate-screen off" in only_command
    assert "set -g mouse on" in only_command


def test_main_extra_provision_command_stacks_with_lima() -> None:
    """`main` + `lima`: lima runs the agent directly in the VM as root, so it runs
    the shared FCT setup scripts via its own `extra_provision_command`. Those must
    stack *after* main's (the timeline is main's tmux config, then the setup
    scripts), and main's command must be preserved.
    """
    result = _apply(("main", "lima"))
    commands = result["extra_provision_command"]
    # main's tmux config still runs.
    assert any(_TMUX_MARKER in cmd for cmd in commands)
    # lima adds the three shared setup scripts, in order, after main's command.
    setup_scripts = ["setup_system.sh", "install_dependencies.sh", "build_workspace.sh"]
    script_positions = [
        next((idx for idx, cmd in enumerate(commands) if script in cmd), -1)
        for script in setup_scripts
    ]
    assert all(pos >= 0 for pos in script_positions), (
        f"missing a lima setup script in {commands!r}"
    )
    assert script_positions == sorted(script_positions), (
        f"lima setup scripts out of order in {commands!r}"
    )
    tmux_position = next(idx for idx, cmd in enumerate(commands) if _TMUX_MARKER in cmd)
    assert tmux_position < script_positions[0], (
        "main's tmux command must run before lima's setup scripts"
    )


def test_main_extra_provision_command_present_for_docker_mode() -> None:
    """`main` + `docker`: docker has no `extra_provision_command` of its own, so only main's runs."""
    result = _apply(("main", "docker"))
    commands = result["extra_provision_command"]
    assert any(_TMUX_MARKER in cmd for cmd in commands)


def test_docker_template_hardens_start_args_and_drops_sys_ptrace() -> None:
    """`main` + `docker`: hardened for untrusted agents -- blocks privilege
    escalation and no longer grants the SYS_PTRACE capability. (The gVisor
    runtime itself is selected via `docker_runtime` in the [providers.docker]
    block, not a create-template setting, so it's not asserted here.)"""
    result = _apply(("main", "docker"))
    # no-new-privileges hardening rides on start_arg (a `docker run` flag).
    assert "--security-opt=no-new-privileges" in result["start_arg"]
    # The SYS_PTRACE capability grant was removed (gVisor is the boundary now).
    assert "--cap-add=SYS_PTRACE" not in result["start_arg"]
    assert "--cap-add=SYS_PTRACE" not in result["build_arg"]


def test_scalar_template_options_override_rather_than_stack() -> None:
    """Scalar-typed options (e.g. provider) get overridden by the latter template."""
    result = _apply(("main", "docker"))
    assert result["provider"] == "docker"
    assert result["target_path"] == "/mngr/code/"
