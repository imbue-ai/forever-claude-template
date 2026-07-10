"""Unit tests for pr_review.prepare (opt-in rich-types state machine).

These never launch a real agent or run a real install: the launcher is injected,
and state is asserted through the on-disk sidecar written under ``tmp_path``.
"""

from pathlib import Path

from pr_review import prepare
from pr_review.github import RepoTree


def _tree(tmp_path: Path) -> RepoTree:
    root = tmp_path / "repo-abc1234"
    root.mkdir()
    return RepoTree(repo="octocat/hello", sha="abc1234", root=root)


def test_status_absent_by_default(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    assert prepare.prepare_status(tree) == {"state": "absent"}
    assert prepare.is_ready(tree) is False
    assert prepare.ready_roots(tree) == []


def test_start_prepare_sets_installing_and_invokes_launcher(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    launched: list[RepoTree] = []
    status = prepare.start_prepare(tree, launcher=launched.append)
    assert status["state"] == "installing"
    assert launched == [tree]
    # The sidecar lives at the tree root (where the agent's cwd is).
    assert (tree.root / prepare.PREP_DIRNAME / "status.json").exists()
    assert prepare.prepare_status(tree)["state"] == "installing"


def test_start_prepare_is_idempotent_while_installing(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    calls: list[RepoTree] = []
    prepare.start_prepare(tree, launcher=calls.append)
    # A second call while installing does not relaunch.
    prepare.start_prepare(tree, launcher=calls.append)
    assert len(calls) == 1


def test_start_prepare_does_not_relaunch_when_ready(tmp_path: Path) -> None:
    tree = _tree(tmp_path)

    def ready_launcher(t: RepoTree) -> None:
        prepare._write_status(t, {"state": "ready", "roots": ["."], "typescript_dir": "."})

    prepare.start_prepare(tree, launcher=ready_launcher)
    assert prepare.is_ready(tree) is True
    assert prepare.ready_roots(tree) == ["."]

    calls: list[RepoTree] = []
    prepare.start_prepare(tree, launcher=calls.append)
    assert calls == []  # already ready -> no relaunch


def test_force_relaunches_even_when_ready(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare._write_status(tree, {"state": "ready", "roots": ["."]})
    calls: list[RepoTree] = []
    prepare.start_prepare(tree, launcher=calls.append, force=True)
    assert calls == [tree]
    assert prepare.prepare_status(tree)["state"] == "installing"


def test_clear_prepared_removes_state_and_node_modules(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare._write_status(tree, {"state": "ready"})
    node_modules = tree.root / "pkg" / "node_modules" / "left-pad"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("module.exports = 1;\n")

    result = prepare.clear_prepared(tree)
    assert result == {"state": "absent"}
    assert not (tree.root / "pkg" / "node_modules").exists()
    assert not (tree.root / prepare.PREP_DIRNAME).exists()
    assert prepare.prepare_status(tree) == {"state": "absent"}


def test_log_tail_reads_recent_lines(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prep = tree.root / prepare.PREP_DIRNAME
    prep.mkdir(parents=True)
    (prep / "prepare.log").write_text("\n".join(f"line {i}" for i in range(100)))
    tail = prepare.log_tail(tree, lines=5)
    assert tail.splitlines() == ["line 95", "line 96", "line 97", "line 98", "line 99"]
