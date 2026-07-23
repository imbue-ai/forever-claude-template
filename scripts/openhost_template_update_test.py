"""Unit tests for the OpenHost template-update detection script.

The decision logic is tested purely; the git-fetch path is exercised against
real throwaway repos in ``tmp_path`` (git is available in the test env), since
that path's whole job is to move a commit between two real repositories.
"""

import subprocess
from pathlib import Path

import openhost_template_update as otu


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.local")
    _git(repo, "config", "user.name", "t")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_is_update_pending_requires_both_shas_present() -> None:
    assert otu.is_update_pending("abc", "def") is True
    assert otu.is_update_pending("abc", "abc") is False
    # Missing either side is never an update (first boot / legacy / unstamped image).
    assert otu.is_update_pending("abc", None) is False
    assert otu.is_update_pending(None, "def") is False
    assert otu.is_update_pending(None, None) is False


def test_read_sha_handles_missing_and_empty(tmp_path: Path) -> None:
    assert otu.read_sha(tmp_path / "nope") is None
    empty = tmp_path / "empty"
    empty.write_text("\n")
    assert otu.read_sha(empty) is None
    good = tmp_path / "good"
    good.write_text("deadbeef\n")
    assert otu.read_sha(good) == "deadbeef"


def test_init_baseline_only_writes_when_unset(tmp_path: Path) -> None:
    baked = tmp_path / "baked"
    baked.write_text("v2\n")
    stored = tmp_path / "stored"

    assert otu.init_baseline(baked_version_path=baked, stored_version_path=stored) is True
    assert stored.read_text().strip() == "v2"

    # Already set: left untouched.
    stored.write_text("v1\n")
    assert otu.init_baseline(baked_version_path=baked, stored_version_path=stored) is False
    assert stored.read_text().strip() == "v1"


def test_mark_reconciled_writes_version_and_clears_marker(tmp_path: Path) -> None:
    stored = tmp_path / "stored"
    pending = tmp_path / "pending"
    pending.write_text("v2\n")
    otu.mark_reconciled(stored_version_path=stored, pending_marker_path=pending, version="v2")
    assert stored.read_text().strip() == "v2"
    assert not pending.exists()
    # Idempotent even when the marker is already gone.
    otu.mark_reconciled(stored_version_path=stored, pending_marker_path=pending, version="v2")


def _capture(tmp_path: Path, workspace: Path, incoming: Path, baked: str, stored: str | None) -> bool:
    baked_path = tmp_path / "baked"
    baked_path.write_text(f"{baked}\n")
    stored_path = tmp_path / "stored"
    if stored is not None:
        stored_path.write_text(f"{stored}\n")
    pending_path = tmp_path / "pending"
    return otu.capture_incoming(
        workspace_dir=workspace,
        incoming_dir=incoming,
        baked_version_path=baked_path,
        stored_version_path=stored_path,
        pending_marker_path=pending_path,
    )


def test_capture_stages_incoming_ref_on_a_real_update(tmp_path: Path) -> None:
    # Workspace descends from a shared base; incoming carries a newer commit.
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    (workspace / "file.txt").write_text("v1\n")
    base_sha = _commit_all(workspace, "base")

    incoming = tmp_path / "incoming"
    _init_repo(incoming)
    _git(workspace, "clone", "-q", str(workspace), str(incoming / "clone"))
    # Build incoming as a clone of the workspace with an extra commit, so they
    # share history (the real seeded-from-image relationship).
    incoming_clone = incoming / "clone"
    (incoming_clone / "file.txt").write_text("v2\n")
    new_sha = _commit_all(incoming_clone, "new template")

    staged = _capture(tmp_path, workspace, incoming_clone, baked=new_sha, stored=base_sha)
    assert staged is True
    # The incoming ref now resolves in the workspace to the new commit.
    resolved = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", otu.INCOMING_REF],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert resolved == new_sha
    assert (tmp_path / "pending").read_text().strip() == new_sha


def test_capture_noops_when_versions_match(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    (workspace / "f").write_text("x")
    sha = _commit_all(workspace, "c")
    incoming = tmp_path / "incoming"
    _init_repo(incoming)
    (incoming / "f").write_text("x")
    _commit_all(incoming, "c")

    assert _capture(tmp_path, workspace, incoming, baked=sha, stored=sha) is False
    assert not (tmp_path / "pending").exists()


def test_capture_noops_without_a_workspace_repo(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()  # exists but is not a git repo (no .git)
    incoming = tmp_path / "incoming"
    _init_repo(incoming)
    (incoming / "f").write_text("x")
    new_sha = _commit_all(incoming, "c")

    assert _capture(tmp_path, workspace, incoming, baked=new_sha, stored="old") is False


def test_capture_marks_pending_when_incoming_source_is_gone(tmp_path: Path) -> None:
    # Versions disagree but the incoming source was already cleaned up: mark
    # pending anyway (best-effort) without fabricating a ref.
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    (workspace / "f").write_text("x")
    _commit_all(workspace, "c")
    missing_incoming = tmp_path / "gone"

    assert _capture(tmp_path, workspace, missing_incoming, baked="v2", stored="v1") is True
    assert (tmp_path / "pending").read_text().strip() == "v2"
