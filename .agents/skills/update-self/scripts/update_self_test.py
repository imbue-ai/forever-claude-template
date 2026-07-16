"""Unit tests for the deterministic update-self helpers.

Covers the pieces the flow relies on being exactly right: target-tag
resolution (latest stable, prereleases excluded, semver not lexical order), the
merged-vs-pulled-in classification, and the path -> change-class mapping.
"""

from __future__ import annotations

import importlib.util
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
