"""Tests for the main entry point."""

from imbue.system_interface.app_context import get_state
from imbue.system_interface.config import Config
from imbue.system_interface.main import _parse_args
from imbue.system_interface.main import build_application


def test_build_application_defaults_have_no_filters() -> None:
    """With no CLI filter args, the app carries no provider/include/exclude filters."""
    args = _parse_args([])
    app = build_application(Config(), args)
    with app.app_context():
        state = get_state()
    assert state.provider_names is None
    assert state.include_filters == ()
    assert state.exclude_filters == ()


def test_build_application_threads_filters_through() -> None:
    """CLI filter args reach the app's state as provider/include/exclude filters."""
    args = _parse_args(
        [
            "--provider",
            "local",
            "--include",
            'state == "RUNNING"',
            "--exclude",
            'name == "test"',
        ]
    )
    app = build_application(Config(), args)
    with app.app_context():
        state = get_state()
    assert state.provider_names == ("local",)
    assert state.include_filters == ('state == "RUNNING"',)
    assert state.exclude_filters == ('name == "test"',)
