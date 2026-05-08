# apps/

Top-level applications for this template. Each app lives in its own subdirectory
with its own `pyproject.toml` and is installed as a `uv tool` from the project
root (see the `extra_provision_command` entries in `.mngr/settings.toml`).

This is distinct from `libs/`, which holds reusable workspace member packages
that the root project depends on directly.
