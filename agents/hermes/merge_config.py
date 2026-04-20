# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Deep-merge a YAML override file on top of a YAML base file.

Used by agents/hermes/setup.sh to overlay the template's hermes config
(model, toolsets, external skill dirs) on top of whatever HERMES_HOME/config.yaml
was seeded from ~/.hermes by the mngr_hermes plugin. Preserves the user's
provider endpoints, API settings, and any other options they had configured.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: Any, override: Any) -> Any:
    """Merge ``override`` onto ``base``.

    Dicts are merged key-by-key recursively. Any other type (scalar, list,
    None) in ``override`` replaces the corresponding value in ``base``.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, override_value in override.items():
            if key in merged:
                merged[key] = deep_merge(merged[key], override_value)
            else:
                merged[key] = override_value
        return merged
    return override


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="Base YAML file (may not exist)")
    parser.add_argument("--override", type=Path, required=True, help="Override YAML file")
    parser.add_argument("--output", type=Path, required=True, help="Destination path")
    args = parser.parse_args()

    base_data: Any = {}
    if args.base.exists():
        with open(args.base) as f:
            base_data = yaml.safe_load(f) or {}
    override_data: Any = {}
    with open(args.override) as f:
        override_data = yaml.safe_load(f) or {}

    merged = deep_merge(base_data, override_data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
