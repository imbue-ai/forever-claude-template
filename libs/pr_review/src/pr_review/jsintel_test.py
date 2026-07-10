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


def test_hover_rejects_prefix_sibling_escape(tmp_path: Path) -> None:
    # A sibling directory whose name shares the tree root's name as a *prefix*
    # must not be treated as inside the tree (a plain string-prefix check would
    # wrongly admit it). Containment is by path boundary, not string prefix.
    tree = write_tree(tmp_path / "myrepo", {"a.ts": "const x = 1;\n"})
    sibling = tmp_path / "myrepo-secret"
    sibling.mkdir()
    (sibling / "leak.ts").write_text("const secret = 1;\n")
    assert jsintel.hover(tree, "../myrepo-secret/leak.ts", line=1, column=7) is None
    assert jsintel.definition(tree, "../myrepo-secret/leak.ts", line=1, column=7) is None


def test_hover_shows_variable_type_annotation(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"main.ts": "const total: number = 3;\nconsole.log(total);\n"},
    )
    # Hover over the ``total`` usage on line 2.
    result = jsintel.hover(tree, "main.ts", line=2, column=13)
    assert result is not None
    assert "const total: number" in result["contents"]


def test_hover_shows_constant_value(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"main.ts": "const CONTENT_PARTITION = 'persist:workspace-content';\nconsole.log(CONTENT_PARTITION);\n"},
    )
    # Hover over the ``CONTENT_PARTITION`` usage on line 2.
    result = jsintel.hover(tree, "main.ts", line=2, column=13)
    assert result is not None
    contents = result["contents"]
    assert "CONTENT_PARTITION" in contents
    assert "'persist:workspace-content'" in contents


def test_hover_omits_long_initializer(tmp_path: Path) -> None:
    long_obj = "{ " + ", ".join(f"k{i}: {i}" for i in range(40)) + " }"  # well over 80 chars
    tree = write_tree(
        tmp_path,
        {"main.ts": f"const CONFIG = {long_obj};\nconsole.log(CONFIG);\n"},
    )
    # A long / bulky initializer is not dumped into the hover; the declaration
    # still shows.
    result = jsintel.hover(tree, "main.ts", line=2, column=13)
    assert result is not None
    contents = result["contents"]
    assert "const CONFIG" in contents
    assert "k39" not in contents


def test_hover_collects_full_line_comment_block(tmp_path: Path) -> None:
    # A run of ``//`` lines is one comment node per line; the whole block above
    # the declaration should surface, not just the last line. The declaration is
    # a ``const``, whose comment is a sibling of the outer lexical_declaration.
    tree = write_tree(
        tmp_path,
        {
            "main.ts": (
                "// First line of the note.\n"
                "// Second line of the note.\n"
                "// Third line of the note.\n"
                "const LIMIT = 50;\n"
            )
        },
    )
    result = jsintel.hover(tree, "main.ts", line=4, column=7)
    assert result is not None
    contents = result["contents"]
    assert "First line of the note." in contents
    assert "Second line of the note." in contents
    assert "Third line of the note." in contents


def test_hover_stops_comment_block_at_blank_line(tmp_path: Path) -> None:
    # A blank line separates an unrelated earlier comment from the declaration's
    # own doc block; only the adjacent block should surface.
    tree = write_tree(
        tmp_path,
        {
            "main.ts": (
                "// Unrelated section header.\n"
                "\n"
                "// Doc for the constant.\n"
                "const LIMIT = 50;\n"
            )
        },
    )
    result = jsintel.hover(tree, "main.ts", line=4, column=7)
    assert result is not None
    contents = result["contents"]
    assert "Doc for the constant." in contents
    assert "Unrelated section header." not in contents


def test_hover_and_definition_for_destructured_require_binding(tmp_path: Path) -> None:
    # CommonJS destructuring from an external module: the name should resolve to
    # its binding (hover shows the shape + source; go-to-def jumps to the line).
    tree = write_tree(
        tmp_path,
        {"main.js": "const { session, app } = require('electron');\nsession.fromPartition('x');\n"},
    )
    hover = jsintel.hover(tree, "main.js", line=2, column=1)
    assert hover is not None
    assert "session" in hover["contents"]
    assert "require('electron')" in hover["contents"]
    definition = jsintel.definition(tree, "main.js", line=2, column=1)
    assert definition is not None
    assert definition["in_repo"] is True
    assert definition["path"] == "main.js"
    assert definition["line"] == 1
    assert definition["name"] == "session"


def test_definition_follows_relative_require(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "util.js": "function helper() {\n  return 1;\n}\nmodule.exports = { helper };\n",
            "main.js": "const { helper } = require('./util');\nhelper();\n",
        },
    )
    # Go to definition of the ``helper`` call on line 2 -> into util.js.
    result = jsintel.definition(tree, "main.js", line=2, column=1)
    assert result is not None
    assert result["in_repo"] is True
    assert result["path"] == "util.js"
    assert result["line"] == 1


def test_definition_returns_none_for_member_access(tmp_path: Path) -> None:
    # A property/method access needs type inference, which a syntactic parser
    # cannot do; it should resolve to nothing rather than a wrong answer.
    tree = write_tree(
        tmp_path,
        {"main.js": "const { session } = require('electron');\nsession.fromPartition('x');\n"},
    )
    # Hover/def on ``fromPartition`` (the member, ~column 9 on line 2).
    assert jsintel.definition(tree, "main.js", line=2, column=9) is None


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


def test_resolves_across_mjs_and_mts_variants(tmp_path: Path) -> None:
    # The .mjs/.cjs/.mts/.cts variants share the JS/TS grammars, so hover and
    # go-to-definition (incl. relative-import following) must work for them too.
    tree = write_tree(
        tmp_path,
        {
            "util.mts": "export function helper(): number {\n  return 1;\n}\n",
            "main.mjs": 'import { helper } from "./util.mts";\n\nhelper();\n',
            "conf.cjs": "const { join } = require('path');\njoin('a', 'b');\n",
        },
    )
    hover = jsintel.hover(tree, "util.mts", line=1, column=17)
    assert hover is not None
    assert "function helper(): number" in hover["contents"]
    definition = jsintel.definition(tree, "main.mjs", line=3, column=1)
    assert definition is not None
    assert definition["in_repo"] is True
    assert definition["path"] == "util.mts"
    assert definition["name"] == "helper"
    # A CommonJS destructure in a .cjs file resolves to its local binding.
    cjs_hover = jsintel.hover(tree, "conf.cjs", line=2, column=1)
    assert cjs_hover is not None
    assert "join" in cjs_hover["contents"]


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
