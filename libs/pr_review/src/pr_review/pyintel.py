"""Type-aware Python intelligence for the diff view, backed by Jedi.

Jedi is a static-analysis library (the engine behind most Python IDE
completion/hover) -- it runs in-process, so there is no language-server process
to manage. We point it at the cached PR source tree as its project root so
within-repo imports resolve, and expose two operations the editor needs: hover
(type + signature + docstring) and go-to-definition.
"""

from pathlib import Path

import jedi

from pr_review.github import RepoTree


def _script(tree: RepoTree, rel_path: str) -> tuple[jedi.Script, int] | None:
    abs_path = (tree.root / rel_path).resolve()
    if not str(abs_path).startswith(str(tree.root.resolve())) or not abs_path.is_file():
        return None
    code = abs_path.read_text(errors="replace")
    project = jedi.Project(str(tree.root))
    return jedi.Script(code=code, path=str(abs_path), project=project), 0


def hover(tree: RepoTree, rel_path: str, line: int, column: int) -> dict | None:
    """Markdown hover for the symbol at (line, column).

    ``line``/``column`` are 1-based (Monaco). Jedi columns are 0-based.
    """
    made = _script(tree, rel_path)
    if not made:
        return None
    script, _ = made
    jcol = max(0, column - 1)
    try:
        names = script.help(line, jcol)
    except (ValueError, IndexError, RecursionError):
        return None
    if not names:
        return None
    name = names[0]
    parts: list[str] = []
    signatures = name.get_signatures()
    if signatures:
        parts.append("```python\n" + signatures[0].to_string() + "\n```")
    elif name.description:
        parts.append("```python\n" + name.description + "\n```")
    full = name.full_name
    if full and full != name.name:
        parts.append("`" + full + "`")
    doc = name.docstring(raw=True)
    if doc:
        parts.append(doc.strip())
    body = "\n\n".join(p for p in parts if p).strip()
    if not body:
        return None
    return {"contents": body}


def definition(tree: RepoTree, rel_path: str, line: int, column: int) -> dict | None:
    """Resolve the definition of the symbol at (line, column).

    Returns ``in_repo`` plus the path (relative to the tree root when in-repo,
    else the absolute path of a stdlib/stub file) so the editor can navigate.
    """
    made = _script(tree, rel_path)
    if not made:
        return None
    script, _ = made
    jcol = max(0, column - 1)
    try:
        defs = script.goto(line, jcol, follow_imports=True, follow_builtin_imports=False)
    except (ValueError, IndexError, RecursionError):
        return None
    for found in defs:
        if not found.module_path:
            continue
        target = Path(found.module_path).resolve()
        root = tree.root.resolve()
        in_repo = str(target).startswith(str(root))
        return {
            "in_repo": in_repo,
            "path": str(target.relative_to(root)) if in_repo else str(target),
            "line": found.line or 1,
            "column": (found.column or 0) + 1,
            "name": found.name,
            "type": found.type,
        }
    return None
