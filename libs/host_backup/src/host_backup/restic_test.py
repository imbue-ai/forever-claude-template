"""Unit tests for host_backup.restic (pure logic only; no subprocess execution)."""

from __future__ import annotations

from host_backup.restic import (
    extract_snapshot_id_from_backup_output,
    is_repo_locked_error,
    is_repo_missing_error,
)


def test_is_repo_missing_error_matches_common_phrases() -> None:
    assert is_repo_missing_error("Fatal: unable to open config file: bla")
    assert is_repo_missing_error("repository does not exist")
    assert is_repo_missing_error("path does not appear to be a repository")


def test_is_repo_missing_error_rejects_unrelated_failures() -> None:
    assert not is_repo_missing_error("network timeout")
    assert not is_repo_missing_error("permission denied")
    assert not is_repo_missing_error("")


def test_is_repo_locked_error_matches_lock_messages() -> None:
    # The exact stderr restic emits for a stale exclusive lock (dead PID).
    assert is_repo_locked_error(
        "unable to create lock in backend: repository is already locked "
        "exclusively by PID 1515556 on 40e922efb0c9 by root (UID 0)"
    )
    assert is_repo_locked_error("repository is already locked by PID 42")


def test_is_repo_locked_error_rejects_unrelated_failures() -> None:
    assert not is_repo_locked_error("network timeout")
    assert not is_repo_locked_error("repository does not exist")
    assert not is_repo_locked_error("")


def test_extract_snapshot_id_returns_summary_id() -> None:
    stdout = (
        '{"message_type":"status","percent_done":0.5}\n'
        '{"message_type":"summary","snapshot_id":"abc123","files_new":10}\n'
    )
    assert extract_snapshot_id_from_backup_output(stdout) == "abc123"


def test_extract_snapshot_id_handles_no_summary() -> None:
    stdout = '{"message_type":"status"}\n'
    assert extract_snapshot_id_from_backup_output(stdout) == ""


def test_extract_snapshot_id_handles_garbage_lines() -> None:
    stdout = 'Loading...\n{"message_type":"summary","snapshot_id":"xyz789"}\n'
    assert extract_snapshot_id_from_backup_output(stdout) == "xyz789"


def test_extract_snapshot_id_handles_empty() -> None:
    assert extract_snapshot_id_from_backup_output("") == ""
