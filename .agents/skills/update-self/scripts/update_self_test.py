"""Unit tests for the deterministic update-self helpers.

Covers the pieces the flow relies on being exactly right: target-tag
resolution (latest stable, prereleases excluded, semver not lexical order), the
merged-vs-pulled-in classification, the path -> change-class mapping, and the
skill bootstrap that extracts the target ref's own copy of the flow.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("update_self.py")
_spec = importlib.util.spec_from_file_location("update_self", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
update_self = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(update_self)


# --- pick_latest_stable_tag / resolve_target -------------------------------


def test_pick_latest_stable_tag_ignores_prereleases() -> None:
    tags = [
        "minds-v0.3.5",
        "minds-v0.3.7",
        "minds-v0.3.7-rc1",
        "minds-v0.3.6",
    ]
    assert update_self.pick_latest_stable_tag(tags) == "minds-v0.3.7"


def test_pick_latest_stable_tag_uses_semver_not_lexical_order() -> None:
    # Lexically "0.3.9" > "0.3.10"; semantically 0.3.10 is newer.
    tags = ["minds-v0.3.9", "minds-v0.3.10", "minds-v0.4.0"]
    assert update_self.pick_latest_stable_tag(tags) == "minds-v0.4.0"
    tags_no_major = ["minds-v0.3.9", "minds-v0.3.10"]
    assert update_self.pick_latest_stable_tag(tags_no_major) == "minds-v0.3.10"


def test_pick_latest_stable_tag_returns_none_when_all_prerelease_or_empty() -> None:
    assert update_self.pick_latest_stable_tag([]) is None
    assert update_self.pick_latest_stable_tag(["minds-v0.3.7-rc1", "v1.2.3"]) is None


def test_resolve_target_defaults_to_latest_stable() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.7", "minds-v0.3.7-rc1"]
    result = update_self.resolve_target(None, tags)
    assert result == update_self.ResolvedTarget("minds-v0.3.7", "tag")


def test_resolve_target_override_main_is_remote_qualified_branch() -> None:
    # Must resolve to the remote branch, not the stale local `main`.
    assert update_self.resolve_target("main", ["minds-v0.3.7"]) == (
        update_self.ResolvedTarget("upstream/main", "branch")
    )
    assert update_self.resolve_target(
        "main", ["minds-v0.3.7"], remote="official"
    ) == update_self.ResolvedTarget("official/main", "branch")


def test_resolve_target_override_known_tag_vs_arbitrary_ref() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.7"]
    assert update_self.resolve_target("minds-v0.3.6", tags).kind == "tag"
    # An override git can validate later but that is not a known tag/main.
    passthrough = update_self.resolve_target("abc1234", tags)
    assert passthrough == update_self.ResolvedTarget("abc1234", "ref")


def test_resolve_target_raises_when_no_stable_tag_and_no_override() -> None:
    try:
        update_self.resolve_target(None, ["minds-v0.3.7-rc1"])
    except ValueError as exc:
        assert "no stable minds-v* tag" in str(exc)
    else:
        raise AssertionError("expected ValueError when no stable tag and no override")


# --- classify_path ---------------------------------------------------------


def test_classify_path_reveal_classes() -> None:
    cases = {
        "apps/system_interface/src/App.tsx": update_self.CLASS_SYSTEM_INTERFACE,
        "supervisord.conf": update_self.CLASS_SERVICE,
        "libs/bootstrap/src/bootstrap/main.py": update_self.CLASS_SERVICE,
        "vendor/mngr/libs/mngr/foo.py": update_self.CLASS_EDITABLE_TOOL,
        "scripts/forward_port.py": update_self.CLASS_SHARED_RUNTIME,
        ".agents/skills/update-self/SKILL.md": update_self.CLASS_SHARED_RUNTIME,
        "libs/oom_priority/src/oom_priority/ledger.py": update_self.CLASS_SHARED_RUNTIME,
        # Provisioning files: pinned-toolchain scripts (would otherwise read as
        # shared_runtime under scripts/) and the .mngr/ create config (would
        # otherwise fall through to other) -- both need the provisioner reveal.
        "scripts/setup_system.sh": update_self.CLASS_PROVISIONER,
        "scripts/install_secret_scanners.sh": update_self.CLASS_PROVISIONER,
        "scripts/_provision_guard.sh": update_self.CLASS_PROVISIONER,
        ".mngr/settings.toml": update_self.CLASS_PROVISIONER,
        "Dockerfile": update_self.CLASS_DOCKERFILE,
        "CLAUDE.md": update_self.CLASS_DOCS,
        "changelog/some-entry.md": update_self.CLASS_DOCS,
        "parent.toml": update_self.CLASS_OTHER,
        # A README is docs even under a prefix with its own reveal class --
        # it must never trigger that class's reveal action (e.g. a service
        # restart for libs/bootstrap/README.md).
        "libs/bootstrap/README.md": update_self.CLASS_DOCS,
        "apps/system_interface/README.md": update_self.CLASS_DOCS,
        "vendor/mngr/README.md": update_self.CLASS_DOCS,
    }
    for path, expected in cases.items():
        assert update_self.classify_path(path).reveal_class == expected, path


def test_classify_path_project_mapping() -> None:
    assert (
        update_self.classify_path("apps/system_interface/foo.py").project
        == "apps/system_interface"
    )
    assert update_self.classify_path("vendor/mngr/x.py").project == "vendor/mngr"
    assert update_self.classify_path("scripts/forward_port.py").project == "."


def test_classify_path_manifest_flag() -> None:
    assert update_self.classify_path("apps/system_interface/pyproject.toml").is_manifest
    assert update_self.classify_path("vendor/mngr/libs/mngr/pyproject.toml").is_manifest
    assert not update_self.classify_path("scripts/forward_port.py").is_manifest


# --- classify_merge --------------------------------------------------------


def test_classify_merge_splits_merged_and_pulled_in() -> None:
    upstream_changed = [
        "apps/system_interface/src/App.tsx",  # also local -> merged
        "scripts/forward_port.py",  # upstream only -> pulled in
        "supervisord.conf",  # upstream only -> pulled in
    ]
    local_changed = [
        "apps/system_interface/src/App.tsx",
        "PURPOSE.md",  # local only, not an upstream update -> ignored
    ]
    result = update_self.classify_merge(upstream_changed, local_changed)

    merged_paths = [entry["path"] for entry in result.merged]
    pulled_paths = [entry["path"] for entry in result.pulled_in]
    assert merged_paths == ["apps/system_interface/src/App.tsx"]
    assert pulled_paths == ["scripts/forward_port.py", "supervisord.conf"]
    # A file only local changed is not surfaced as an upstream update at all.
    assert "PURPOSE.md" not in merged_paths + pulled_paths


def test_classify_merge_summary_fields() -> None:
    upstream_changed = [
        "apps/system_interface/src/App.tsx",  # merged
        "vendor/mngr/libs/mngr/foo.py",  # merged
        "scripts/forward_port.py",  # pulled in
    ]
    local_changed = [
        "apps/system_interface/src/App.tsx",
        "vendor/mngr/libs/mngr/foo.py",
    ]
    result = update_self.classify_merge(upstream_changed, local_changed)
    assert result.reveal_classes_merged == [
        update_self.CLASS_EDITABLE_TOOL,
        update_self.CLASS_SYSTEM_INTERFACE,
    ]
    assert result.reveal_classes_pulled_in == [update_self.CLASS_SHARED_RUNTIME]
    assert result.projects_to_validate == ["apps/system_interface", "vendor/mngr"]


def test_classify_merge_surfaces_provisioner_bump() -> None:
    # The motivating case: upstream bumps the pinned latchkey version in
    # scripts/setup_system.sh and touches .mngr/settings.toml, local left both
    # untouched. They come in as a clean pull, but must still surface under the
    # provisioner reveal class (not shared_runtime/other) so the flow re-runs the
    # provisioner or flags a rebuild rather than silently dropping the new pin.
    result = update_self.classify_merge(
        ["scripts/setup_system.sh", ".mngr/settings.toml"], []
    )
    assert result.reveal_classes_pulled_in == [update_self.CLASS_PROVISIONER]
    assert [entry["reveal_class"] for entry in result.pulled_in] == [
        update_self.CLASS_PROVISIONER,
        update_self.CLASS_PROVISIONER,
    ]


def test_classify_merge_empty() -> None:
    result = update_self.classify_merge([], [])
    assert result.merged == []
    assert result.pulled_in == []
    assert result.projects_to_validate == []


# --- CLI wiring --------------------------------------------------------------


def test_repo_root_flag_accepted_before_and_after_subcommand(tmp_path, capsys) -> None:
    # `--repo-root` must work both before and after the subcommand. Each
    # ordering has broken in its own way: a value after the subcommand errored
    # when the option lived only on the top parser, and a value *before* it was
    # silently clobbered back to cwd by the subparser's default on
    # Python < 3.13 (bpo-9351). Asserting on the resolved tag (which only
    # exists in the tmp repo) catches both -- a clobber would resolve against
    # the real repo and either fail or print a different ref.
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git(
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "root",
    )
    _git("tag", "minds-v0.1.0")

    for argv in (
        ["resolve-target", "--local-tags", "--repo-root", str(tmp_path)],
        ["--repo-root", str(tmp_path), "resolve-target", "--local-tags"],
    ):
        assert update_self.main(argv) == 0, argv
        assert '"minds-v0.1.0"' in capsys.readouterr().out, argv


# --- read_tree / trees_differ ----------------------------------------------


def test_trees_differ_detects_content_and_file_set_changes(tmp_path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "sub").mkdir(parents=True)
        (root / "SKILL.md").write_text("same", encoding="utf-8")
        (root / "sub" / "a.py").write_text("print(1)", encoding="utf-8")
    assert not update_self.trees_differ(left, right)

    # A content change on one side is a difference.
    (right / "SKILL.md").write_text("changed", encoding="utf-8")
    assert update_self.trees_differ(left, right)

    # A differing file set is a difference even when shared files match.
    (right / "SKILL.md").write_text("same", encoding="utf-8")
    (right / "extra.md").write_text("new", encoding="utf-8")
    assert update_self.trees_differ(left, right)


def test_trees_differ_missing_tree_counts_as_empty(tmp_path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    (present / "SKILL.md").write_text("x", encoding="utf-8")
    assert update_self.trees_differ(present, tmp_path / "absent")


# --- bootstrap-skill --------------------------------------------------------


def _init_repo_with_skill(root: Path, skill_body: str) -> None:
    """Init a git repo at ``root`` carrying the update-self skill, tagged v1."""

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    skill_dir = root / update_self.SKILL_DIR_REL
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_body, encoding="utf-8")
    (skill_dir / "scripts" / "update_self.py").write_text("# v1\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-q", "-m", "add skill")
    _git("tag", "minds-v1.0.0")


def test_bootstrap_skill_extracts_tag_copy_and_flags_difference(
    tmp_path, capsys
) -> None:
    # The tag carries the "original" skill; local then edits SKILL.md, so the
    # bootstrap must extract the *tag's* copy (unchanged body) and report that it
    # differs from the drifted local copy.
    repo = tmp_path / "repo"
    _init_repo_with_skill(repo, skill_body="ORIGINAL FLOW\n")
    (repo / update_self.SKILL_DIR_REL / "SKILL.md").write_text(
        "LOCALLY EDITED FLOW\n", encoding="utf-8"
    )

    dest = tmp_path / "staging"
    assert (
        update_self.main(
            [
                "bootstrap-skill",
                "--ref",
                "minds-v1.0.0",
                "--dest",
                str(dest),
                "--repo-root",
                str(repo),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["differs"] is True
    assert payload["ref"] == "minds-v1.0.0"
    staged_skill = Path(payload["skill_dir"])
    # The staged copy is the tag's content, not the drifted local edit.
    assert staged_skill.joinpath("SKILL.md").read_text() == "ORIGINAL FLOW\n"


def test_bootstrap_skill_reports_no_difference_when_local_matches_tag(
    tmp_path, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo_with_skill(repo, skill_body="STABLE FLOW\n")

    assert (
        update_self.main(
            [
                "bootstrap-skill",
                "--ref",
                "minds-v1.0.0",
                "--dest",
                str(tmp_path / "staging"),
                "--repo-root",
                str(repo),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["differs"] is False


def test_bootstrap_skill_reports_null_when_ref_predates_skill(
    tmp_path, capsys
) -> None:
    # A ref with no update-self skill at all yields no staged copy, so the caller
    # falls back to the local flow instead of trying to follow a missing one.
    repo = tmp_path / "repo"

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    repo.mkdir()
    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    _git("commit", "--allow-empty", "-q", "-m", "root")
    _git("tag", "minds-v0.0.1")

    assert (
        update_self.main(
            [
                "bootstrap-skill",
                "--ref",
                "minds-v0.0.1",
                "--dest",
                str(tmp_path / "staging"),
                "--repo-root",
                str(repo),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["skill_dir"] is None
    assert payload["differs"] is False
