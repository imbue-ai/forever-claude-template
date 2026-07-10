"""Unit tests for pr_review.jsintel (tree-sitter JS/TS hover and go-to-definition).

These parse small JS/TS trees built in ``tmp_path`` for real -- no network and no
GitHub access are involved.
"""

from pathlib import Path

from pr_review import jsintel
from pr_review.testing import write_tree


def test_hover_returns_signature_and_doc_comment(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"util.ts": "/** Add two numbers. */\nexport function add(a: number, b: number): number {\n  return a + b;\n}\n"},
    )
    # Hover over the ``add`` name in its own declaration (line 2, col 17).
    result = jsintel.hover(tree, "util.ts", line=2, column=17)
    assert result is not None
    contents = result["contents"]
    assert "function add(a: number, b: number): number" in contents
    assert "Add two numbers." in contents
    # The body is not part of the signature.
    assert "return a + b" not in contents


def test_hover_returns_none_for_non_js_ts_file(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"mod.py": "x = 1\n"})
    assert jsintel.hover(tree, "mod.py", line=1, column=1) is None


def test_hover_returns_none_for_path_escape(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"a.ts": "const x = 1;\n"})
    assert jsintel.hover(tree, "../outside.ts", line=1, column=1) is None


def test_hover_returns_none_for_missing_file(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"a.ts": "const x = 1;\n"})
    assert jsintel.hover(tree, "nope.ts", line=1, column=1) is None


def test_hover_shows_variable_type_annotation(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"main.ts": "const total: number = 3;\nconsole.log(total);\n"},
    )
    # Hover over the ``total`` usage on line 2.
    result = jsintel.hover(tree, "main.ts", line=2, column=13)
    assert result is not None
    assert "const total: number" in result["contents"]


def test_definition_resolves_import_across_files(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "util.ts": "export function helper(): number {\n  return 1;\n}\n",
            "main.ts": 'import { helper } from "./util";\n\nhelper();\n',
        },
    )
    # Go to definition of the ``helper`` call in main.ts (line 3, col 1).
    result = jsintel.definition(tree, "main.ts", line=3, column=1)
    assert result is not None
    assert result["in_repo"] is True
    assert result["path"] == "util.ts"
    assert result["line"] == 1
    assert result["name"] == "helper"
    assert result["type"] == "function"


def test_definition_resolves_default_import(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "widget.ts": "export default class Widget {\n  render(): void {}\n}\n",
            "app.ts": 'import Widget from "./widget";\n\nnew Widget();\n',
        },
    )
    # Go to definition of the ``Widget`` usage in app.ts (line 3, col 5).
    result = jsintel.definition(tree, "app.ts", line=3, column=5)
    assert result is not None
    assert result["in_repo"] is True
    assert result["path"] == "widget.ts"
    assert result["line"] == 1
    assert result["type"] == "class"


def test_definition_resolves_local_parameter(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"main.ts": "function use(x: string): void {\n  console.log(x);\n}\n"},
    )
    # Go to definition of the ``x`` usage on line 2.
    result = jsintel.definition(tree, "main.ts", line=2, column=15)
    assert result is not None
    assert result["in_repo"] is True
    assert result["path"] == "main.ts"
    assert result["line"] == 1
    assert result["name"] == "x"
    assert result["type"] == "parameter"


def test_definition_works_for_plain_javascript(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"a.js": "function greet(n) {\n  return 'hi' + n;\n}\ngreet('x');\n"},
    )
    # Go to definition of the ``greet`` call on line 4.
    result = jsintel.definition(tree, "a.js", line=4, column=1)
    assert result is not None
    assert result["path"] == "a.js"
    assert result["line"] == 1
    assert result["name"] == "greet"
    assert result["type"] == "function"


def test_definition_returns_none_for_external_import(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"main.ts": 'import { useState } from "react";\n\nuseState();\n'},
    )
    # A bare (non-relative) specifier is not in the repo tree.
    assert jsintel.definition(tree, "main.ts", line=3, column=1) is None


def test_definition_returns_none_for_missing_file(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"main.ts": "const x = 1;\n"})
    assert jsintel.definition(tree, "ghost.ts", line=1, column=1) is None


def test_definition_resolves_tsx_component(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "Button.tsx": "export function Button(): null {\n  return null;\n}\n",
            "App.tsx": 'import { Button } from "./Button";\n\nconst el = Button();\n',
        },
    )
    # Go to definition of the ``Button`` usage in App.tsx (line 3).
    result = jsintel.definition(tree, "App.tsx", line=3, column=12)
    assert result is not None
    assert result["path"] == "Button.tsx"
    assert result["type"] == "function"
