"""Tests for the model-picker catalog and settings.json reader."""

import json
from pathlib import Path

from imbue.system_interface.model_settings import DEFAULT_MODEL_ID
from imbue.system_interface.model_settings import base_alias
from imbue.system_interface.model_settings import is_valid_model_id
from imbue.system_interface.model_settings import read_model_settings
from imbue.system_interface.model_settings import supports_fast_mode


def test_catalog_ids_are_accepted_and_others_rejected() -> None:
    assert is_valid_model_id("opus[1m]")
    assert is_valid_model_id("fable")
    assert is_valid_model_id("sonnet")
    assert is_valid_model_id("haiku")
    # The catalog exposes Opus as the 1M variant only, so the bare alias is not a valid id.
    assert not is_valid_model_id("opus")
    assert not is_valid_model_id("gpt-4")
    assert not is_valid_model_id("")


def test_base_alias_strips_context_variant() -> None:
    assert base_alias("opus[1m]") == "opus"
    assert base_alias("opus") == "opus"
    assert base_alias("Sonnet") == "sonnet"


def test_only_opus_supports_fast_mode() -> None:
    # Matched by bare alias, so a stored "opus" and "opus[1m]" both count as Opus.
    assert supports_fast_mode("opus[1m]")
    assert supports_fast_mode("opus")
    assert not supports_fast_mode("sonnet")
    assert not supports_fast_mode("fable")
    assert not supports_fast_mode("haiku")


def test_read_model_settings_reads_model_and_fast_mode(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"model": "sonnet", "fastMode": True}))
    assert read_model_settings(settings_path) == ("sonnet", True)


def test_read_model_settings_defaults_when_keys_absent(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"theme": "dark"}))
    assert read_model_settings(settings_path) == (DEFAULT_MODEL_ID, False)


def test_read_model_settings_defaults_on_missing_or_unreadable_file(tmp_path: Path) -> None:
    assert read_model_settings(tmp_path / "nope.json") == (DEFAULT_MODEL_ID, False)
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    assert read_model_settings(bad) == (DEFAULT_MODEL_ID, False)


def test_read_model_settings_ignores_wrong_typed_values(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    # A non-string model and a non-bool fastMode fall back to the defaults.
    settings_path.write_text(json.dumps({"model": 123, "fastMode": "yes"}))
    assert read_model_settings(settings_path) == (DEFAULT_MODEL_ID, False)
