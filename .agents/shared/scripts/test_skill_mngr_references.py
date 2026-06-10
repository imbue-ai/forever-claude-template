"""Guard: ``mngr <subcommand>`` references in skill markdown name real commands.

Skills carry ``mngr ...`` command examples in their prose that agents copy and
run verbatim. When vendor/mngr renames or removes a subcommand, those examples
go stale silently. This test scans skill markdown for code-formatted
``mngr <subcommand>`` tokens and asserts each subcommand exists in the live mngr
CLI, so that drift fails at merge.

Scope and limitations (deliberate, to keep the guard low-false-positive):
  - Only tokens inside inline code spans or fenced code blocks are considered;
    plain-prose mentions ("the mngr CLI handles ...") are ignored.
  - Subcommand-level only -- it does not parse flags out of prose. Full
    argv-level contract checking lives in the argv-builder tests.
  - Plugin-provided subcommands (e.g. ``mngr file``, ``mngr wait``) are not on
    the base CLI and are not installed in CI's venv, so they are allow-listed
    explicitly below. A genuinely removed *core* command (like ``push``) is in
    neither set and is therefore caught.
"""

from __future__ import annotations

import re
from pathlib import Path

from imbue.mngr.main import cli

# This test lives at .agents/shared/scripts/<file>, so the repo root is three
# directories up; the two skill-doc roots it scans are .agents/skills and
# .agents/shared.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_MD_ROOTS = (_REPO_ROOT / ".agents" / "skills", _REPO_ROOT / ".agents" / "shared")

# Subcommands provided by mngr plugins (mngr_file, mngr_wait, ...). These are
# registered at runtime via the register_cli_commands pluggy hook and are not
# part of the base CLI that the test venv installs, so we cannot resolve them
# against ``cli`` -- but they are still valid things for a skill to reference.
# Keep this list to commands actually mentioned in skill prose.
_KNOWN_PLUGIN_SUBCOMMANDS = frozenset({"file", "wait"})

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FENCED_CODE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# `mngr` followed by whitespace then a word starting with a letter (the
# subcommand). Excludes `mngr/<branch>`, `mngr_claude`, `$MNGR_*`, `mngr --flag`.
_MNGR_SUBCOMMAND = re.compile(r"\bmngr\s+([a-zA-Z][\w-]*)")


def _valid_subcommands() -> frozenset[str]:
    return frozenset(cli.commands.keys()) | _KNOWN_PLUGIN_SUBCOMMANDS


def _code_regions(text: str) -> list[str]:
    return _FENCED_CODE.findall(text) + [m.group(1) for m in _INLINE_CODE.finditer(text)]


def _iter_skill_markdown() -> list[Path]:
    return [p for root in _SKILL_MD_ROOTS for p in root.rglob("*.md")]


def test_skill_markdown_mngr_subcommands_exist() -> None:
    valid = _valid_subcommands()
    offenders: list[str] = []
    scanned = 0
    for md in _iter_skill_markdown():
        scanned += 1
        for region in _code_regions(md.read_text(encoding="utf-8")):
            for match in _MNGR_SUBCOMMAND.finditer(region):
                subcommand = match.group(1)
                if subcommand not in valid:
                    offenders.append(f"{md.relative_to(_REPO_ROOT)}: `mngr {subcommand}`")

    # Vacuity guard: the skills genuinely use mngr, so the scan must have walked
    # real files. (A zero here would mean the globs broke, not that all is well.)
    assert scanned > 0, "no skill markdown found -- check _SKILL_MD_ROOTS"
    assert not offenders, (
        "Skill markdown references mngr subcommands that the live CLI does not "
        "have (and that are not known plugin commands). The vendored mngr CLI "
        "likely renamed/removed them -- update the skill prose (or, for a new "
        "plugin command, add it to _KNOWN_PLUGIN_SUBCOMMANDS):\n  "
        + "\n  ".join(offenders)
    )
