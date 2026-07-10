"""Syntax-aware JavaScript / TypeScript intelligence for the diff view.

The Python side (``pyintel``) uses Jedi; the JS/TS side uses tree-sitter. Both
run in-process (tree-sitter is a compiled parser, so -- like Jedi -- there is no
language-server process to manage) and expose the same two operations the editor
needs: hover (declaration signature + doc comment) and go-to-definition.

tree-sitter is a *parser*, not a type checker, so resolution is by declaration +
import-following across the cached repo tree rather than full type inference. In
practice that is close to what the editor wants here: for TypeScript the
declarations already carry the author's type annotations, so hover surfaces real
types, and go-to-definition follows relative imports to the declaring file.

Covers ``.ts``/``.tsx``/``.mts``/``.cts`` and ``.js``/``.jsx``/``.mjs``/``.cjs``.
"""

from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser

from pr_review.github import RepoTree

# File extension -> tree-sitter grammar name. The ``tsx`` grammar is a superset
# of ``typescript`` that also parses JSX; the ``javascript`` grammar handles JSX
# too, so plain ``.js``/``.jsx`` share it.
_GRAMMAR_BY_EXT = {
    "ts": "typescript",
    "mts": "typescript",
    "cts": "typescript",
    "tsx": "tsx",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
}

# Identifier-ish leaf node types the cursor can land on and we can resolve.
_IDENT_TYPES = frozenset(
    {
        "identifier",
        "type_identifier",
        "property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
        "private_property_identifier",
    }
)

# Named declaration node types -> the friendly "kind" reported to the editor.
_NAMED_DECL_KINDS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "method_definition": "method",
}

# Function-like scopes, which additionally bind their parameters.
_FUNC_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function_expression",
        "arrow_function",
        "method_definition",
    }
)

# Module resolution suffixes, in preference order (TS before JS).
_MODULE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


class _Decl(NamedTuple):
    """A resolved declaration: enough to render a hover and to navigate to it.

    ``container`` is the full declaration node (used for the hover signature);
    ``name_node`` is the identifier naming it (used for the go-to location).
    ``path`` is repo-relative and ``in_repo`` says whether it lives in the tree.
    """

    source: bytes
    container: Node
    name_node: Node
    kind: str
    name: str
    path: str
    in_repo: bool


def _grammar_for(name: str) -> str | None:
    return _GRAMMAR_BY_EXT.get(name.rsplit(".", 1)[-1].lower())


@lru_cache(maxsize=8)
def _parser(grammar: str) -> Parser:
    return get_parser(grammar)


def _resolve_in_tree(tree: RepoTree, rel_path: str) -> tuple[Path, str] | None:
    """Validate ``rel_path`` stays inside the tree and maps to a JS/TS grammar."""
    grammar = _grammar_for(rel_path)
    if grammar is None:
        return None
    abs_path = (tree.root / rel_path).resolve()
    if not str(abs_path).startswith(str(tree.root.resolve())) or not abs_path.is_file():
        return None
    return abs_path, grammar


def _point(source: bytes, line: int, column: int) -> tuple[int, int]:
    """Convert Monaco's 1-based (line, column) to a tree-sitter 0-based byte point."""
    row = line - 1
    lines = source.split(b"\n")
    if row < 0 or row >= len(lines):
        return (max(0, row), max(0, column - 1))
    text = lines[row].decode("utf-8", errors="replace")
    char_col = max(0, column - 1)
    return (row, len(text[:char_col].encode("utf-8")))


def _col_char(source: bytes, point: tuple[int, int]) -> int:
    """Inverse of :func:`_point` for the column: byte offset -> 1-based char column."""
    row, byte_col = point
    lines = source.split(b"\n")
    if row < 0 or row >= len(lines):
        return 1
    return len(lines[row][:byte_col].decode("utf-8", errors="replace")) + 1


def _identifier_at(root: Node, point: tuple[int, int]) -> Node | None:
    node = root.descendant_for_point_range(point, point)
    if node is not None and node.type in _IDENT_TYPES:
        return node
    return None


def _string_value(node: Node) -> str:
    for child in node.named_children:
        if child.type == "string_fragment":
            return child.text.decode("utf-8", errors="replace")
    return node.text.decode("utf-8", errors="replace").strip("'\"`")


def _decl_name_node(decl: Node) -> Node | None:
    """The identifier naming ``decl``, or None if it has no simple name."""
    if decl.type in _NAMED_DECL_KINDS:
        return decl.child_by_field_name("name")
    if decl.type == "variable_declarator":
        name = decl.child_by_field_name("name")
        return name if name is not None and name.type == "identifier" else None
    return None


def _decl_kind(decl: Node) -> str:
    if decl.type == "variable_declarator":
        parent = decl.parent
        kind = parent.child_by_field_name("kind") if parent is not None else None
        return "constant" if kind is not None and kind.text == b"const" else "variable"
    return _NAMED_DECL_KINDS.get(decl.type, "symbol")


def _binding_target(decl: Node) -> Node:
    return _decl_name_node(decl) or decl


def _iter_params(func: Node) -> list[tuple[str, Node, Node]]:
    """(name, identifier-node, parameter-node) for each named parameter of ``func``."""
    params = func.child_by_field_name("parameters")
    if params is None:
        return []
    out: list[tuple[str, Node, Node]] = []
    for param in params.named_children:
        ident: Node | None = None
        if param.type == "identifier":
            ident = param
        elif param.type in ("required_parameter", "optional_parameter"):
            pattern = param.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                ident = pattern
        if ident is not None:
            out.append((ident.text.decode("utf-8", errors="replace"), ident, param))
    return out


def _iter_scope_bindings(scope: Node) -> list[tuple[str, Node]]:
    """(name, declaration-container) for declarations directly in ``scope``.

    Unwraps ``export`` statements and expands ``const``/``let``/``var`` groups so
    ``export const x = ...`` and ``function f() {}`` both surface as bindings.
    """
    out: list[tuple[str, Node]] = []
    for child in scope.named_children:
        node = child
        if node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is None:
                continue
            node = decl
        if node.type in ("lexical_declaration", "variable_declaration"):
            for declarator in node.named_children:
                if declarator.type == "variable_declarator":
                    name = _decl_name_node(declarator)
                    if name is not None:
                        out.append((name.text.decode("utf-8", errors="replace"), declarator))
        elif node.type in _NAMED_DECL_KINDS:
            name = _decl_name_node(node)
            if name is not None:
                out.append((name.text.decode("utf-8", errors="replace"), node))
    return out


def _find_top_binding(root: Node, name: str) -> Node | None:
    for candidate, decl in _iter_scope_bindings(root):
        if candidate == name:
            return decl
    return None


def _find_default_export(root: Node) -> Node | None:
    for stmt in root.named_children:
        if stmt.type != "export_statement":
            continue
        if not any(child.type == "default" for child in stmt.children):
            continue
        return stmt.child_by_field_name("declaration")
    return None


def _collect_imports(root: Node) -> dict[str, tuple[str, str]]:
    """local-name -> (module-specifier, imported-name), where imported-name is the
    real export name, ``"default"`` for a default import, or ``"*"`` for a namespace.
    """
    imports: dict[str, tuple[str, str]] = {}
    for stmt in root.named_children:
        if stmt.type != "import_statement":
            continue
        source = stmt.child_by_field_name("source")
        if source is None:
            continue
        spec = _string_value(source)
        clause = next((c for c in stmt.named_children if c.type == "import_clause"), None)
        if clause is None:
            continue
        for member in clause.named_children:
            if member.type == "identifier":
                imports[member.text.decode("utf-8", errors="replace")] = (spec, "default")
            elif member.type == "namespace_import":
                ident = next((n for n in member.named_children if n.type == "identifier"), None)
                if ident is not None:
                    imports[ident.text.decode("utf-8", errors="replace")] = (spec, "*")
            elif member.type == "named_imports":
                for specifier in member.named_children:
                    if specifier.type != "import_specifier":
                        continue
                    name_node = specifier.child_by_field_name("name")
                    if name_node is None:
                        continue
                    alias_node = specifier.child_by_field_name("alias")
                    imported = name_node.text.decode("utf-8", errors="replace")
                    local = (alias_node or name_node).text.decode("utf-8", errors="replace")
                    imports[local] = (spec, imported)
    return imports


def _resolve_module(tree_root: Path, from_rel: str, spec: str) -> Path | None:
    """Resolve a *relative* import specifier to a file inside the tree, or None.

    Bare specifiers (``react``, ``@scope/pkg``) are external -- not in the tree --
    so they resolve to None. Tries the path itself, then each JS/TS extension,
    then an ``index.*`` file for directory imports.
    """
    if not (spec.startswith(".") or spec.startswith("/")):
        return None
    root_res = tree_root.resolve()
    if spec.startswith("/"):
        base = tree_root / spec.lstrip("/")
    else:
        base = (tree_root / from_rel).parent / spec
    candidates: list[Path] = [base]
    candidates += [Path(str(base) + ext) for ext in _MODULE_EXTS]
    candidates += [base / f"index{ext}" for ext in _MODULE_EXTS]
    for candidate in candidates:
        resolved = candidate.resolve()
        if str(resolved).startswith(str(root_res)) and resolved.is_file():
            return resolved
    return None


def _resolve_local(source: bytes, rel_path: str, ident: Node, name: str) -> _Decl | None:
    """Find ``name``'s binding by walking scopes outward from ``ident``."""
    node: Node | None = ident
    while node is not None:
        if node.type in _FUNC_TYPES:
            for param_name, param_ident, param_node in _iter_params(node):
                if param_name == name:
                    return _Decl(source, param_node, param_ident, "parameter", name, rel_path, True)
        if node.type in ("program", "statement_block", "class_body"):
            for binding_name, decl in _iter_scope_bindings(node):
                if binding_name == name:
                    return _Decl(source, decl, _binding_target(decl), _decl_kind(decl), name, rel_path, True)
        node = node.parent
    return None


def _resolve_import(tree: RepoTree, from_rel: str, entry: tuple[str, str], local_name: str) -> _Decl | None:
    spec, imported = entry
    target = _resolve_module(tree.root, from_rel, spec)
    if target is None:
        return None
    grammar = _grammar_for(target.name)
    if grammar is None:
        return None
    tsource = target.read_bytes()
    troot = _parser(grammar).parse(tsource).root_node
    rel = str(target.resolve().relative_to(tree.root.resolve()))
    if imported == "*":
        return _Decl(tsource, troot, troot, "module", local_name, rel, True)
    container = _find_default_export(troot) if imported == "default" else _find_top_binding(troot, imported)
    if container is None:
        # Module resolved but the specific export was not located (e.g. a
        # re-export); still let the editor jump to the file.
        return _Decl(tsource, troot, troot, "module", local_name, rel, True)
    return _Decl(tsource, container, _binding_target(container), _decl_kind(container), local_name, rel, True)


def _resolve(tree: RepoTree, rel_path: str, line: int, column: int) -> _Decl | None:
    resolved = _resolve_in_tree(tree, rel_path)
    if resolved is None:
        return None
    abs_path, grammar = resolved
    source = abs_path.read_bytes()
    root = _parser(grammar).parse(source).root_node
    ident = _identifier_at(root, _point(source, line, column))
    if ident is None:
        return None
    name = ident.text.decode("utf-8", errors="replace")
    imports = _collect_imports(root)
    if name in imports:
        return _resolve_import(tree, rel_path, imports[name], name)
    return _resolve_local(source, rel_path, ident, name)


def _bounded(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _signature(container: Node, source: bytes) -> str:
    """A one-declaration signature string (no body) for the hover code block."""

    def text_of(start: int, end: int) -> str:
        return source[start:end].decode("utf-8", errors="replace").strip()

    kind = container.type
    if kind in _FUNC_TYPES or kind in ("class_declaration", "abstract_class_declaration"):
        body = container.child_by_field_name("body")
        end = body.start_byte if body is not None else container.end_byte
        return _bounded(text_of(container.start_byte, end))
    if kind == "variable_declarator":
        parent = container.parent
        keyword_node = parent.child_by_field_name("kind") if parent is not None else None
        keyword = keyword_node.text.decode() if keyword_node is not None else "const"
        value = container.child_by_field_name("value")
        if value is not None and value.type in ("arrow_function", "function_expression"):
            inner = value.child_by_field_name("body")
            end = inner.start_byte if inner is not None else container.end_byte
            return _bounded(f"{keyword} {text_of(container.start_byte, end)}")
        name_node = container.child_by_field_name("name")
        type_node = container.child_by_field_name("type")
        signature = f"{keyword} {name_node.text.decode() if name_node else ''}"
        if type_node is not None:
            signature += type_node.text.decode()
        return _bounded(signature)
    if kind in ("required_parameter", "optional_parameter", "identifier"):
        return _bounded(text_of(container.start_byte, container.end_byte))
    return _bounded(text_of(container.start_byte, container.end_byte))


def _clean_comment(text: str) -> str:
    text = text.strip()
    if text.startswith("/*"):
        text = text[2:]
        if text.endswith("*/"):
            text = text[:-2]
        lines = [line.strip().lstrip("*").strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()
    if text.startswith("//"):
        return text.lstrip("/").strip()
    return text


# Wrappers between a declaration and the statement whose sibling the doc comment
# is: a ``const``/``let``/``var`` declarator sits inside a (possibly exported)
# lexical/variable declaration, so the comment is a sibling of that outer node,
# not of the declarator itself.
_STATEMENT_WRAPPERS = frozenset({"lexical_declaration", "variable_declaration", "export_statement"})


def _statement_node(container: Node) -> Node:
    """Climb out of declarator / export wrappers to the statement that carries the
    preceding doc comment as a sibling."""
    node = container
    while node.parent is not None and node.parent.type in _STATEMENT_WRAPPERS:
        node = node.parent
    return node


def _leading_comment(container: Node, source: bytes) -> str | None:
    """The full contiguous block of comment lines directly above the declaration.

    A ``/** ... */`` block is a single comment node, but a run of ``//`` lines is
    one node per line, so we walk backwards collecting every comment that is
    directly above the previous one (no blank-line gap) and join them in source
    order. A blank line ends the block, so an unrelated earlier comment is left
    out.
    """
    node = _statement_node(container)
    comments: list[Node] = []
    anchor_row = node.start_point[0]
    prev = node.prev_sibling
    while prev is not None and prev.type == "comment" and anchor_row - prev.end_point[0] <= 1:
        comments.append(prev)
        anchor_row = prev.start_point[0]
        prev = prev.prev_sibling
    if not comments:
        return None
    comments.reverse()
    cleaned = (_clean_comment(node.text.decode("utf-8", errors="replace")) for node in comments)
    joined = "\n".join(part for part in cleaned if part).strip()
    return joined or None


def hover(tree: RepoTree, rel_path: str, line: int, column: int) -> dict | None:
    """Markdown hover for the symbol at (line, column). 1-based, Monaco-style."""
    decl = _resolve(tree, rel_path, line, column)
    if decl is None or decl.kind == "module":
        return None
    parts: list[str] = []
    signature = _signature(decl.container, decl.source)
    if signature:
        parts.append("```typescript\n" + signature + "\n```")
    doc = _leading_comment(decl.container, decl.source)
    if doc:
        parts.append(doc)
    body = "\n\n".join(part for part in parts if part).strip()
    return {"contents": body} if body else None


def definition(tree: RepoTree, rel_path: str, line: int, column: int) -> dict | None:
    """Resolve the definition of the symbol at (line, column). 1-based, Monaco-style."""
    decl = _resolve(tree, rel_path, line, column)
    if decl is None:
        return None
    point = decl.name_node.start_point
    return {
        "in_repo": decl.in_repo,
        "path": decl.path,
        "line": point[0] + 1,
        "column": _col_char(decl.source, point),
        "name": decl.name,
        "type": decl.kind,
    }
