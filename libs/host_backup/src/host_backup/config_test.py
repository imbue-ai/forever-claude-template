"""Unit tests for host_backup.config."""

from __future__ import annotations

from pathlib import Path

from host_backup.capabilities import SnapshotMethod
from host_backup.config import (
    BackupConfig,
    SnapshotSettings,
    load_backup_config,
    load_restic_env,
    merge_snapshot_into_existing_toml,
    missing_required_restic_keys,
    parse_restic_env_file,
    render_default_backup_toml,
    write_default_restic_env_template,
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


def test_missing_required_restic_keys_reports_repo_and_password_when_empty() -> None:
    assert sorted(missing_required_restic_keys({})) == [
        "RESTIC_PASSWORD",
        "RESTIC_REPOSITORY",
    ]


def test_missing_required_restic_keys_reports_empty_when_repo_and_password_set() -> (
    None
):
    env = {
        "RESTIC_REPOSITORY": "s3:https://acct.r2.cloudflarestorage.com/bucket",
        "RESTIC_PASSWORD": "p",
    }
    assert missing_required_restic_keys(env) == []


def test_missing_required_restic_keys_does_not_require_aws_creds() -> None:
    # Backend creds are not gated here; restic reports its own error if a
    # given backend needs one. Only repo + password are required.
    env = {"RESTIC_REPOSITORY": "s3:host/bucket", "RESTIC_PASSWORD": "p"}
    assert missing_required_restic_keys(env) == []


def test_missing_required_restic_keys_treats_empty_value_as_missing() -> None:
    env = {"RESTIC_REPOSITORY": "", "RESTIC_PASSWORD": "p"}
    assert missing_required_restic_keys(env) == ["RESTIC_REPOSITORY"]


# --- load_backup_config (tolerant loading) ---


def test_load_backup_config_returns_defaults_when_absent(tmp_path: Path) -> None:
    config = load_backup_config(tmp_path / "missing.toml")
    assert config == BackupConfig()


def test_load_backup_config_returns_defaults_on_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[snapshot\nbroken")
    assert load_backup_config(path) == BackupConfig()


def test_load_backup_config_applies_user_settings(tmp_path: Path) -> None:
    path = tmp_path / "backup.toml"
    path.write_text(
        "backup_interval_seconds = 1800\n"
        "excludes = ['**/only-this']\n"
        "[retention]\n"
        "keep_hourly = 48\n"
    )
    config = load_backup_config(path)
    assert config.backup_interval_seconds == 1800.0
    assert config.excludes == ("**/only-this",)
    assert config.retention.keep_hourly == 48
    # Untouched fields keep their defaults.
    assert config.retention.keep_daily == 30
    assert config.minimum_backup_gap_seconds == 60.0


def test_load_backup_config_ignores_unknown_keys_but_applies_the_rest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backup.toml"
    path.write_text("no_such_setting = true\nbackup_interval_seconds = 120\n")
    config = load_backup_config(path)
    assert config.backup_interval_seconds == 120.0


def test_load_backup_config_ignores_stale_snapshot_section(tmp_path: Path) -> None:
    # Old bootstraps rewrite a [snapshot] section forever; it must be ignored
    # (capabilities are detected at runtime) without disturbing user settings.
    path = tmp_path / "backup.toml"
    path.write_text(
        "backup_interval_seconds = 240\n"
        "[snapshot]\n"
        "method = 'outer_trigger'\n"
        "trigger_dir = '/mngr-snapshot'\n"
    )
    config = load_backup_config(path)
    assert config.backup_interval_seconds == 240.0


def test_load_backup_config_skips_invalid_value_but_applies_valid_ones(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backup.toml"
    path.write_text(
        "backup_interval_seconds = 'not-a-number'\nminimum_backup_gap_seconds = 5\n"
    )
    config = load_backup_config(path)
    # The malformed field falls back to its default; the valid one applies.
    assert config.backup_interval_seconds == 3600.0
    assert config.minimum_backup_gap_seconds == 5.0


def test_load_backup_config_skips_invalid_retention_but_applies_valid_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "backup.toml"
    path.write_text(
        "backup_interval_seconds = 300\n[retention]\nkeep_hourly = 'lots'\n"
    )
    config = load_backup_config(path)
    assert config.backup_interval_seconds == 300.0
    assert config.retention.keep_hourly == 24


# --- backwards-compatibility shims for pre-refactor bootstraps ---


def _shim_snapshot_settings() -> SnapshotSettings:
    # Constructed exactly the way an old bootstrap constructs it.
    return SnapshotSettings(
        method=SnapshotMethod.OUTER_TRIGGER,
        btrfs_mount_path=Path("/mngr-btrfs"),
        host_subvolume_path=Path("/mngr-btrfs/<host_id_hex>"),
        snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
        snapshot_read_path=Path("/mngr-snapshots/current"),
        trigger_dir=Path("/mngr-snapshot"),
    )


def test_shim_write_default_restic_env_template_never_writes(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "restic.env"
    assert write_default_restic_env_template(path) is False
    assert not path.exists()


def test_shim_merge_snapshot_into_existing_toml_returns_text_unchanged() -> None:
    existing = "backup_interval_seconds = 77\n[snapshot]\nmethod = 'direct'\n"
    assert merge_snapshot_into_existing_toml(existing, _shim_snapshot_settings()) == (
        existing
    )


def test_shim_render_default_backup_toml_is_comment_only(tmp_path: Path) -> None:
    rendered = render_default_backup_toml(_shim_snapshot_settings())
    stripped_lines = [line for line in rendered.splitlines() if line.strip()]
    assert stripped_lines
    assert all(line.startswith("#") for line in stripped_lines)
    # An old bootstrap may write this to backup.toml when the file is absent;
    # it must load as an all-defaults config.
    path = tmp_path / "backup.toml"
    path.write_text(rendered)
    assert load_backup_config(path) == BackupConfig()
