"""Unit tests for pr_review.prepare (opt-in rich-types state machine).

These never launch a real agent or run a real install: the launcher is injected,
and state is asserted through the on-disk sidecar written under ``tmp_path``.
"""

from pathlib import Path

import pytest

from pr_review import prepare
from pr_review.github import RepoTree
from pr_review.testing import seed_prepared_state


def _tree(tmp_path: Path) -> RepoTree:
    root = tmp_path / "repo-abc1234"
    root.mkdir()
    return RepoTree(repo="octocat/hello", sha="abc1234", root=root)


def _tree_at(tmp_path: Path, sha: str, deps: dict[str, str]) -> RepoTree:
    """A checkout dir under ``tmp_path`` seeded with the given dependency files."""
    root = tmp_path / f"repo-{sha}"
    root.mkdir()
    for rel, content in deps.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return RepoTree(repo="octocat/hello", sha=sha, root=root)


def _seed_prepared(
    tree: RepoTree, roots: list[str], notes: str = "used pnpm; engine-strict fallback"
) -> None:
    """Fake a completed install on ``tree``: ready sidecar + a typescript@5 + node_modules."""
    seed_prepared_state(tree, roots, notes=notes)


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


def test_normalize_model_validates() -> None:
    assert prepare.normalize_model("claude-opus-4-8") == "claude-opus-4-8"
    assert prepare.normalize_model(None) == prepare.DEFAULT_MODEL
    assert prepare.normalize_model("gpt-4") == prepare.DEFAULT_MODEL


def test_start_prepare_records_chosen_model(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare.start_prepare(tree, launcher=lambda _t: None, model="claude-opus-4-8")
    assert prepare.prepare_status(tree)["model"] == "claude-opus-4-8"


def test_start_prepare_defaults_invalid_model(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare.start_prepare(tree, launcher=lambda _t: None, model="nonsense")
    assert prepare.prepare_status(tree)["model"] == prepare.DEFAULT_MODEL


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
        prepare._write_status(
            t, {"state": "ready", "roots": ["."], "typescript_dir": "."}
        )

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


def test_dep_fingerprint_reflects_deps_not_source(tmp_path: Path) -> None:
    tree = _tree_at(
        tmp_path,
        "sha1",
        {"package.json": '{"deps":1}', "package-lock.json": '{"lock":1}'},
    )
    fp = prepare.dep_fingerprint(tree.root)
    assert fp is not None
    # Non-dependency source changes do not affect the fingerprint.
    (tree.root / "app.js").write_text("console.log(1)\n")
    (tree.root / "src").mkdir()
    (tree.root / "src" / "index.ts").write_text("export const x = 1\n")
    assert prepare.dep_fingerprint(tree.root) == fp
    # Installed artifacts are ignored, even when they contain package.json files.
    nm = tree.root / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name":"left-pad"}')
    assert prepare.dep_fingerprint(tree.root) == fp
    # A real dependency change flips it.
    (tree.root / "package.json").write_text('{"deps":2}')
    assert prepare.dep_fingerprint(tree.root) != fp


def test_dep_fingerprint_none_without_dep_files(tmp_path: Path) -> None:
    tree = _tree_at(tmp_path, "sha1", {"README.md": "# hi\n"})
    assert prepare.dep_fingerprint(tree.root) is None


def test_dep_fingerprint_matches_across_checkouts(tmp_path: Path) -> None:
    deps = {"package.json": '{"deps":1}', "pnpm-lock.yaml": "lockfile: 6\n"}
    a = _tree_at(tmp_path, "sha_a", deps)
    b = _tree_at(tmp_path, "sha_b", {**deps, "unrelated.py": "x = 1\n"})
    assert prepare.dep_fingerprint(a.root) == prepare.dep_fingerprint(b.root)


def test_start_prepare_reuses_published_prep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # store paths are relative to cwd
    deps = {"package.json": '{"deps":1}', "package-lock.json": '{"lock":1}'}
    producer = _tree_at(tmp_path, "sha_producer", deps)
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    # A different checkout with identical deps reuses without launching the agent.
    consumer = _tree_at(tmp_path, "sha_consumer", deps)
    launched: list[RepoTree] = []
    status = prepare.start_prepare(consumer, launcher=launched.append)

    assert launched == []  # no agent run
    assert status["state"] == "ready"
    assert prepare.is_ready(consumer) is True
    # Artifacts are symlinks into the shared store, not fresh installs.
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()
    linked_nm = consumer.root / "node_modules"
    assert linked_nm.is_symlink()
    assert (linked_nm / "left-pad" / "index.js").exists()


def test_start_prepare_no_reuse_for_different_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    producer = _tree_at(tmp_path, "sha_p", {"package.json": '{"deps":1}'})
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    consumer = _tree_at(tmp_path, "sha_c", {"package.json": '{"deps":2}'})
    launched: list[RepoTree] = []
    status = prepare.start_prepare(consumer, launcher=launched.append)
    assert launched == [consumer]  # different deps -> real install
    assert status["state"] == "installing"


def test_clear_prepared_unlinks_reused_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_p", deps)
    _seed_prepared(producer, ["."])
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])

    consumer = _tree_at(tmp_path, "sha_c", deps)
    prepare.start_prepare(consumer, launcher=lambda _t: None)
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()

    result = prepare.clear_prepared(consumer)
    assert result == {"state": "absent"}
    assert not (consumer.root / prepare.PREP_DIRNAME).exists()
    assert not (consumer.root / "node_modules").exists()
    # The shared store survives so other checkouts keep reusing it.
    assert prepare._entry_is_ready(prepare._store_entry(consumer.repo, fp))


def test_force_reprepare_detaches_store_links_and_leaves_store_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_p", deps)
    _seed_prepared(producer, ["."])
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])

    # A consumer reuses the published prep: its sidecar + node_modules are now
    # symlinks pointing into the shared store.
    consumer = _tree_at(tmp_path, "sha_c", deps)
    prepare.start_prepare(consumer, launcher=lambda _t: None)
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()
    assert (consumer.root / "node_modules").is_symlink()

    # A forced re-prepare must NOT write the "installing" status (nor later run the
    # installer) through those symlinks into the shared store. The store entry it
    # was symlinked to stays ready, and the checkout's own paths are detached.
    launched: list[RepoTree] = []
    status = prepare.start_prepare(consumer, launcher=launched.append, force=True)
    assert status["state"] == "installing"
    assert launched == [consumer]
    assert not (consumer.root / prepare.PREP_DIRNAME).is_symlink()  # local real dir now
    assert not (consumer.root / "node_modules").is_symlink()
    # The shared store entry is untouched -- other checkouts keep reusing it.
    assert prepare._entry_is_ready(prepare._store_entry(consumer.repo, fp))


def test_auto_enable_materializes_exact_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_old", deps)
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    consumer = _tree_at(tmp_path, "sha_new", deps)  # same deps, never enabled
    status = prepare.auto_enable(consumer)
    assert status["state"] == "ready"
    assert prepare.is_ready(consumer) is True
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()


def test_auto_enable_noop_without_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    producer = _tree_at(tmp_path, "sha_old", {"package.json": '{"deps":1}'})
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    # Different deps: an install would be needed, so auto-enable does nothing.
    consumer = _tree_at(tmp_path, "sha_new", {"package.json": '{"deps":2}'})
    status = prepare.auto_enable(consumer)
    assert status == {"state": "absent"}
    assert not (consumer.root / prepare.PREP_DIRNAME).exists()


def test_auto_enable_leaves_installing_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tree = _tree_at(tmp_path, "sha", {"package.json": '{"deps":1}'})
    prepare._write_status(tree, {"state": "installing"})
    assert prepare.auto_enable(tree)["state"] == "installing"


def test_reusable_entry_short_circuits_when_repo_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tree = _tree_at(tmp_path, "sha", {"package.json": '{"deps":1}'})
    # No prep has ever been published for this repo, so its store dir is absent
    # and the lookup returns without fingerprinting the tree.
    assert not (prepare.PREP_STORE / prepare._safe_slug(tree.repo)).exists()
    assert prepare.reusable_entry(tree) is None
    assert prepare.auto_enable(tree) == {"state": "absent"}


def test_link_creates_replaces_dir_and_replaces_symlink(tmp_path: Path) -> None:
    source = tmp_path / "store" / "node_modules"
    source.mkdir(parents=True)
    (source / "marker.txt").write_text("from store\n")
    target = tmp_path / "checkout" / "node_modules"

    # Fresh create: the target does not exist yet.
    prepare._link(target, source)
    assert target.is_symlink()
    assert (target / "marker.txt").read_text() == "from store\n"

    # Replace a real directory sitting where the link should go (a prior install).
    target.unlink()
    target.mkdir()
    (target / "stale.txt").write_text("old install\n")
    prepare._link(target, source)
    assert target.is_symlink()
    assert not (target / "stale.txt").exists()
    assert (target / "marker.txt").read_text() == "from store\n"

    # Replace an existing symlink that points somewhere else (a stale reuse).
    other = tmp_path / "other" / "node_modules"
    other.mkdir(parents=True)
    target.unlink()
    target.symlink_to(other.resolve())
    prepare._link(target, source)
    assert target.is_symlink()
    assert target.resolve() == source.resolve()


def test_seed_for_install_copies_prior_and_returns_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # A prior prep for the repo, under some other dependency fingerprint.
    producer = _tree_at(tmp_path, "sha_old", {"package.json": '{"deps":1}'})
    _seed_prepared(producer, ["apps/minds"], notes="engine-strict fallback to npm")
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["apps/minds"])

    # A new checkout with *different* deps: no exact reuse, so we seed the install.
    consumer = _tree_at(
        tmp_path, "sha_new", {"package.json": '{"deps":2}', "apps/minds/x": ""}
    )
    fp = prepare.dep_fingerprint(consumer.root)
    hint = prepare._seed_for_install(consumer, fp, model="claude-haiku-4-5")

    assert hint is not None
    assert (
        "pnpm" in hint
        and "apps/minds" in hint
        and "engine-strict fallback to npm" in hint
    )
    # Seeded artifacts are real writable copies (the agent mutates them), not symlinks.
    prep = consumer.root / prepare.PREP_DIRNAME
    assert prep.is_dir() and not prep.is_symlink()
    assert (prep / "node_modules" / "typescript" / "package.json").exists()
    nm = consumer.root / "apps/minds" / "node_modules"
    assert nm.is_dir() and not nm.is_symlink()
    # The stale prior status/result are dropped; status is back to installing.
    assert not (prep / "agent_result.json").exists()
    assert prepare.prepare_status(consumer)["state"] == "installing"


def test_seed_for_install_returns_none_without_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    consumer = _tree_at(tmp_path, "sha_new", {"package.json": '{"deps":1}'})
    fp = prepare.dep_fingerprint(consumer.root)
    assert prepare._seed_for_install(consumer, fp, model="claude-haiku-4-5") is None


def test_seed_for_install_skips_exact_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_old", deps)
    _seed_prepared(producer, ["."])
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])

    # A checkout with the SAME fingerprint would be reused, not seeded -- so the only
    # available prior (the exact match) is skipped and there is nothing to seed.
    consumer = _tree_at(tmp_path, "sha_new", deps)
    assert prepare._seed_for_install(consumer, fp, model="claude-haiku-4-5") is None


def test_build_prompt_prepends_hint_only_when_present() -> None:
    assert prepare._build_prompt(None) == prepare._AGENT_PROMPT
    withhint = prepare._build_prompt("do X first")
    assert withhint.startswith("PRIOR PREPARATION CONTEXT")
    assert "do X first" in withhint and prepare._AGENT_PROMPT in withhint


def test_log_tail_reads_recent_lines(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prep = tree.root / prepare.PREP_DIRNAME
    prep.mkdir(parents=True)
    (prep / "prepare.log").write_text("\n".join(f"line {i}" for i in range(100)))
    tail = prepare.log_tail(tree, lines=5)
    assert tail.splitlines() == ["line 95", "line 96", "line 97", "line 98", "line 99"]
