"""Unit tests for pr_review.tsintel (rich-types Python client).

These never launch a real Node process: a fake server factory is injected. The
real language-service path is exercised manually / in the release check.
"""

from pathlib import Path

import pytest

from pr_review import tsintel
from pr_review.github import RepoTree


def _tree(tmp_path: Path) -> RepoTree:
    root = tmp_path / "repo-abc1234"
    root.mkdir()
    return RepoTree(repo="octocat/hello", sha="abc1234", root=root)


class _FakeServer:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.last_used = 0.0
        self.closed = False

    def request(self, payload: dict) -> dict:
        return self._responder(payload)

    def alive(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True


def _factory(responder):
    return lambda _tree: _FakeServer(responder)


@pytest.fixture(autouse=True)
def _clean_registry():
    tsintel.reset()
    yield
    tsintel.reset()


def test_hover_translates_contents(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    responder = lambda p: {"contents": "```typescript\nconst x: number\n```"}
    result = tsintel.hover(tree, "a.ts", 1, 1, server_factory=_factory(responder))
    assert result == {"contents": "```typescript\nconst x: number\n```"}


def test_hover_empty_contents_is_none(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    result = tsintel.hover(tree, "a.ts", 1, 1, server_factory=_factory(lambda p: {"contents": ""}))
    assert result is None


def test_hover_error_is_none(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    result = tsintel.hover(tree, "a.ts", 1, 1, server_factory=_factory(lambda p: {"error": "boom"}))
    assert result is None


def test_hover_none_when_server_cannot_start(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    assert tsintel.hover(tree, "a.ts", 1, 1, server_factory=lambda _t: None) is None


def test_definition_maps_found_result(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    responder = lambda p: {
        "found": True, "in_repo": True, "path": "util.ts",
        "line": 3, "column": 5, "name": "helper", "type": "function",
    }
    result = tsintel.definition(tree, "main.ts", 1, 1, server_factory=_factory(responder))
    assert result == {
        "in_repo": True, "path": "util.ts", "line": 3, "column": 5,
        "name": "helper", "type": "function",
    }


def test_definition_not_found_is_none(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    result = tsintel.definition(tree, "main.ts", 1, 1, server_factory=_factory(lambda p: {"found": False}))
    assert result is None


def test_transport_error_drops_server_and_returns_none(tmp_path: Path) -> None:
    tree = _tree(tmp_path)

    def boom(_payload):
        raise tsintel._ServerError("pipe broke")

    assert tsintel.hover(tree, "a.ts", 1, 1, server_factory=_factory(boom)) is None
    # The dead server was dropped from the registry.
    assert str(tree.root) not in tsintel._servers


def test_server_is_reused_across_calls(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    starts = {"n": 0}

    def factory(_t):
        starts["n"] += 1
        return _FakeServer(lambda p: {"contents": "x"})

    tsintel.hover(tree, "a.ts", 1, 1, server_factory=factory)
    tsintel.hover(tree, "a.ts", 2, 1, server_factory=factory)
    assert starts["n"] == 1  # spawned once, reused
