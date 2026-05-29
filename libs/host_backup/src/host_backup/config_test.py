"""Unit tests for host_backup.config."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import tomlkit

from host_backup.config import (
    BackupConfig,
    BackupConfigError,
    SnapshotMethod,
    SnapshotSettings,
    load_backup_config,
    load_restic_env,
    merge_snapshot_into_existing_toml,
    missing_required_restic_keys,
    parse_restic_env_file,
    render_default_backup_toml,
    resolve_host_id,
    resolve_repository_url,
    write_default_restic_env_template,
)


def _direct_snapshot() -> SnapshotSettings:
    return SnapshotSettings(method=SnapshotMethod.DIRECT)


def _outer_trigger_snapshot() -> SnapshotSettings:
    return SnapshotSettings(
        method=SnapshotMethod.OUTER_TRIGGER,
        btrfs_mount_path=Path("/mngr-btrfs"),
        host_subvolume_path=Path("/mngr-btrfs/deadbeef"),
        snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
        snapshot_read_path=Path("/mngr-snapshots/current"),
        trigger_dir=Path("/mngr-snapshot"),
    )


def _btrfs_local_snapshot() -> SnapshotSettings:
    return SnapshotSettings(
        method=SnapshotMethod.BTRFS_LOCAL,
        btrfs_mount_path=Path("/mnt/host-volume"),
        host_subvolume_path=Path("/mnt/host-volume/host_dir"),
        snapshot_current_path=Path("/mnt/host-volume/snapshots/current"),
        snapshot_read_path=Path("/mnt/host-volume/snapshots/current"),
    )


# --- parse_restic_env_file ---


def test_parse_restic_env_file_handles_plain_keys() -> None:
    parsed = parse_restic_env_file(
        "RESTIC_PASSWORD=hunter2\nAWS_ACCESS_KEY_ID=AKIAEXAMPLE\n"
    )
    assert parsed == {"RESTIC_PASSWORD": "hunter2", "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"}


def test_parse_restic_env_file_strips_quotes_and_export() -> None:
    parsed = parse_restic_env_file(
        "export RESTIC_PASSWORD=\"pa ss\"\nexport OTHER='val'\n"
    )
    assert parsed == {"RESTIC_PASSWORD": "pa ss", "OTHER": "val"}


def test_parse_restic_env_file_ignores_comments_and_blanks() -> None:
    parsed = parse_restic_env_file("# top\n\nA=1\n  # indented comment\nB=2\n")
    assert parsed == {"A": "1", "B": "2"}


def test_parse_restic_env_file_ignores_keyless_lines() -> None:
    assert parse_restic_env_file("=novalue\nGOOD=value\n") == {"GOOD": "value"}


# --- load_restic_env ---


def test_load_restic_env_returns_empty_when_absent(tmp_path: Path) -> None:
    assert load_restic_env(tmp_path / "missing.env") == {}


def test_load_restic_env_reads_existing(tmp_path: Path) -> None:
    env_path = tmp_path / "restic.env"
    env_path.write_text("RESTIC_PASSWORD=p\nAWS_ACCESS_KEY_ID=k\n")
    assert load_restic_env(env_path) == {
        "RESTIC_PASSWORD": "p",
        "AWS_ACCESS_KEY_ID": "k",
    }


# --- missing_required_restic_keys ---


def test_missing_required_restic_keys_reports_all_missing() -> None:
    assert sorted(missing_required_restic_keys({})) == [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "RESTIC_PASSWORD",
    ]


def test_missing_required_restic_keys_reports_empty_when_all_set() -> None:
    env = {
        "RESTIC_PASSWORD": "p",
        "AWS_ACCESS_KEY_ID": "k",
        "AWS_SECRET_ACCESS_KEY": "s",
    }
    assert missing_required_restic_keys(env) == []


def test_missing_required_restic_keys_treats_empty_value_as_missing() -> None:
    env = {
        "RESTIC_PASSWORD": "p",
        "AWS_ACCESS_KEY_ID": "",
        "AWS_SECRET_ACCESS_KEY": "s",
    }
    assert missing_required_restic_keys(env) == ["AWS_ACCESS_KEY_ID"]


# --- resolve_repository_url ---


def test_resolve_repository_url_substitutes_host_id_and_values() -> None:
    config = BackupConfig(
        snapshot=_direct_snapshot(),
        restic={
            "repository_url_template": (
                "s3:https://{account_id}.r2.cloudflarestorage.com/{bucket}/{host_id}"
            ),
            "template_values": {"account_id": "ABC", "bucket": "my-bucket"},
        },
    )
    url = resolve_repository_url(config.restic, host_id="agent-123")
    assert url == "s3:https://ABC.r2.cloudflarestorage.com/my-bucket/agent-123"


def test_resolve_repository_url_raises_on_missing_field() -> None:
    config = BackupConfig(
        snapshot=_direct_snapshot(),
        restic={
            "repository_url_template": "s3:https://{missing_field}/{host_id}",
            "template_values": {},
        },
    )
    with pytest.raises(BackupConfigError) as excinfo:
        resolve_repository_url(config.restic, host_id="agent-123")
    assert "missing_field" in str(excinfo.value)


# --- resolve_host_id ---


def test_resolve_host_id_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-XYZ")
    assert resolve_host_id() == "agent-XYZ"


def test_resolve_host_id_falls_back_to_host_dir_data_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"host_id": "host-from-data"}))
    assert resolve_host_id() == "host-from-data"


def test_resolve_host_id_raises_when_nothing_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    with pytest.raises(BackupConfigError):
        resolve_host_id()


def test_resolve_host_id_rejects_path_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "agent/../other")
    with pytest.raises(BackupConfigError) as excinfo:
        resolve_host_id()
    assert "illegal characters" in str(excinfo.value)


# --- write_default_restic_env_template ---


def test_write_default_restic_env_template_creates_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "restic.env"
    assert write_default_restic_env_template(path) is True
    assert path.exists()
    assert "RESTIC_PASSWORD" in path.read_text()


def test_write_default_restic_env_template_is_noop_when_present(tmp_path: Path) -> None:
    path = tmp_path / "restic.env"
    path.write_text("user content\n")
    assert write_default_restic_env_template(path) is False
    assert path.read_text() == "user content\n"


def test_write_default_restic_env_template_does_not_set_live_keys(
    tmp_path: Path,
) -> None:
    """The template must be all commented out so script refuses to run unless user fills it in."""
    path = tmp_path / "restic.env"
    write_default_restic_env_template(path)
    parsed = parse_restic_env_file(path.read_text())
    # No active (uncommented) keys should be present in the template.
    assert parsed == {}


# --- render_default_backup_toml ---


def test_render_default_backup_toml_parses_into_valid_config() -> None:
    rendered = render_default_backup_toml(_outer_trigger_snapshot())
    parsed = tomllib.loads(rendered)
    config = BackupConfig.model_validate(parsed)
    assert config.snapshot.method == SnapshotMethod.OUTER_TRIGGER
    assert config.snapshot.trigger_dir == Path("/mngr-snapshot")
    assert config.retention.keep_hourly == 24
    assert "**/.venv" in config.excludes


# --- merge_snapshot_into_existing_toml ---


def test_merge_snapshot_into_existing_toml_preserves_user_fields() -> None:
    existing = render_default_backup_toml(_direct_snapshot())
    # User edits retention + excludes + repo URL.
    existing_doc = tomlkit.parse(existing)
    existing_doc["retention"]["keep_hourly"] = 48
    existing_doc["restic"]["template_values"]["bucket"] = "user-bucket"
    user_edited = tomlkit.dumps(existing_doc)

    merged = merge_snapshot_into_existing_toml(user_edited, _outer_trigger_snapshot())
    parsed = tomllib.loads(merged)
    # Snapshot section was rewritten:
    assert parsed["snapshot"]["method"] == SnapshotMethod.OUTER_TRIGGER.value
    assert parsed["snapshot"]["trigger_dir"] == "/mngr-snapshot"
    # User edits are preserved:
    assert parsed["retention"]["keep_hourly"] == 48
    assert parsed["restic"]["template_values"]["bucket"] == "user-bucket"


def test_merge_snapshot_into_existing_toml_drops_optional_paths_for_direct() -> None:
    existing = render_default_backup_toml(_outer_trigger_snapshot())
    merged = merge_snapshot_into_existing_toml(existing, _direct_snapshot())
    parsed = tomllib.loads(merged)
    assert parsed["snapshot"]["method"] == SnapshotMethod.DIRECT.value
    assert "trigger_dir" not in parsed["snapshot"]
    assert "snapshot_current_path" not in parsed["snapshot"]


# --- load_backup_config ---


def test_load_backup_config_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BackupConfigError):
        load_backup_config(tmp_path / "missing.toml")


def test_load_backup_config_raises_on_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[snapshot\nbroken")
    with pytest.raises(BackupConfigError):
        load_backup_config(path)


def test_load_backup_config_round_trips_default_template(tmp_path: Path) -> None:
    path = tmp_path / "backup.toml"
    path.write_text(render_default_backup_toml(_btrfs_local_snapshot()))
    config = load_backup_config(path)
    assert config.snapshot.method == SnapshotMethod.BTRFS_LOCAL
    assert config.snapshot.host_subvolume_path == Path("/mnt/host-volume/host_dir")
