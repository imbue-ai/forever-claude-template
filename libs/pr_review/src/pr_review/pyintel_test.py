"""Unit tests for pr_review.pyintel (Jedi-backed hover and go-to-definition).

These run Jedi for real against small Python trees built in ``tmp_path`` -- no
network and no GitHub access are involved.
"""

from pathlib import Path

from pr_review import pyintel
from pr_review.testing import write_tree


def test_hover_returns_signature_and_docstring(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"mod.py": 'def greet(name: str) -> str:\n    """Say hello to someone."""\n    return "hi " + name\n\n\ngreet("world")\n'},
    )
    # Hover over the ``greet`` call on the last line (1-based line 6, column 1).
    result = pyintel.hover(tree, "mod.py", line=6, column=1)
    assert result is not None
    contents = result["contents"]
    assert "greet" in contents
    assert "Say hello to someone." in contents


def test_hover_returns_none_on_empty_location(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"mod.py": "\n\n"})
    assert pyintel.hover(tree, "mod.py", line=1, column=1) is None


def test_hover_returns_none_for_path_escape(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"mod.py": "x = 1\n"})
    assert pyintel.hover(tree, "../outside.py", line=1, column=1) is None


def test_hover_returns_none_for_missing_file(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"mod.py": "x = 1\n"})
    assert pyintel.hover(tree, "nope.py", line=1, column=1) is None


def test_definition_resolves_in_repo_symbol_across_files(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "defs.py": "def helper() -> int:\n    return 1\n",
            "main.py": "from defs import helper\n\nhelper()\n",
        },
    )
    # Go to definition of the ``helper`` call in main.py (1-based line 3, col 1).
    result = pyintel.definition(tree, "main.py", line=3, column=1)
    assert result is not None
    assert result["in_repo"] is True
    assert result["path"] == "defs.py"
    assert result["line"] == 1
    assert result["name"] == "helper"
    assert result["type"] == "function"


def test_definition_returns_none_for_missing_file(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"main.py": "x = 1\n"})
    assert pyintel.definition(tree, "ghost.py", line=1, column=1) is None
