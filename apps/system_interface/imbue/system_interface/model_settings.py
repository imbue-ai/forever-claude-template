"""The catalog of selectable Claude Code models and the read side of the composer
model picker.

Claude Code exposes no stable programmatic list of the models a session can
switch to, so the catalog below is maintained by hand to match the aliases
``claude --model`` accepts (``fable`` / ``opus`` / ``sonnet`` / ``haiku`` -- see
``claude --help``). The picker sends the chosen id straight to Claude Code's
``/model`` command; Claude Code applies it to the running session and persists it
to the agent's ``settings.json`` ``model`` field, which is also where we read the
current selection back from (``read_model_settings``).
"""

import json
from pathlib import Path

from imbue.system_interface.models import ModelOption

# Opus uses the ``[1m]`` variant to keep the 1M-token context window the workspace
# provisions agents with; picking plain ``opus`` would silently drop to the
# standard window. Fast mode is an Opus-only capability (the ``/fast`` command).
MODEL_OPTIONS: tuple[ModelOption, ...] = (
    ModelOption(id="fable", label="Fable 5", supports_fast_mode=False),
    ModelOption(id="opus[1m]", label="Opus 4.8", supports_fast_mode=True),
    ModelOption(id="sonnet", label="Sonnet 5", supports_fast_mode=False),
    ModelOption(id="haiku", label="Haiku 4.5", supports_fast_mode=False),
)

# What the picker shows when an agent's settings.json has no explicit ``model``
# (matches the workspace's provisioning default -- Opus with the 1M window).
DEFAULT_MODEL_ID = "opus[1m]"

_VALID_MODEL_IDS: frozenset[str] = frozenset(option.id for option in MODEL_OPTIONS)


def is_valid_model_id(model_id: str) -> bool:
    """True when ``model_id`` is one of the catalog's selectable model ids."""
    return model_id in _VALID_MODEL_IDS


def base_alias(model: str) -> str:
    """Reduce a model string to its bare alias for matching.

    Claude Code stamps context/variant suffixes onto the alias in settings.json
    (e.g. ``opus[1m]``). Stripping the ``[...]`` suffix lets a stored ``opus`` or
    ``opus[1m]`` both match the catalog's Opus option.
    """
    return model.split("[", 1)[0].strip().lower()


def supports_fast_mode(model: str) -> bool:
    """True when ``model`` (a raw settings value or a catalog id) is a fast-mode model."""
    alias = base_alias(model)
    return any(base_alias(option.id) == alias and option.supports_fast_mode for option in MODEL_OPTIONS)


def read_model_settings(settings_path: Path) -> tuple[str, bool]:
    """Read ``(model, fast_mode)`` from a Claude Code ``settings.json``.

    A missing file, unreadable JSON, or absent keys fall back to the default
    model and fast mode off, so the picker still renders (just showing defaults)
    for an agent whose settings have not been written yet.
    """
    try:
        raw = settings_path.read_text()
    except OSError:
        return (DEFAULT_MODEL_ID, False)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (DEFAULT_MODEL_ID, False)
    model = data.get("model")
    if not isinstance(model, str) or not model:
        model = DEFAULT_MODEL_ID
    fast_mode = data.get("fastMode")
    return (model, fast_mode if isinstance(fast_mode, bool) else False)
