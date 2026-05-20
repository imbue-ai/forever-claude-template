"""Tests for the main entry point."""

from unittest.mock import patch

from imbue.system_interface.config import Config
from imbue.system_interface.main import main


def test_main_starts_server() -> None:
    """main() creates an app and starts uvicorn."""
    with (
        patch("imbue.system_interface.main.load_config") as mock_load_config,
        patch("imbue.system_interface.main.create_application") as mock_create_app,
        patch("imbue.system_interface.main.uvicorn") as mock_uvicorn,
        patch("sys.argv", ["system-interface"]),
    ):
        mock_config = Config()
        mock_load_config.return_value = mock_config
        mock_create_app.return_value = "fake_app"

        main()

        mock_load_config.assert_called_once()
        mock_create_app.assert_called_once_with(
            mock_config,
            provider_names=None,
            include_filters=(),
            exclude_filters=(),
        )
        mock_uvicorn.run.assert_called_once_with(
            "fake_app",
            host="127.0.0.1",
            port=8000,
        )


def test_main_passes_filters() -> None:
    """main() passes CLI filter args to create_application."""
    with (
        patch("imbue.system_interface.main.load_config") as mock_load_config,
        patch("imbue.system_interface.main.create_application") as mock_create_app,
        patch("imbue.system_interface.main.uvicorn"),
        patch(
            "sys.argv",
            [
                "system-interface",
                "--provider",
                "local",
                "--include",
                'state == "RUNNING"',
                "--exclude",
                'name == "test"',
            ],
        ),
    ):
        mock_load_config.return_value = Config()
        mock_create_app.return_value = "fake_app"

        main()

        mock_create_app.assert_called_once()
        call_kwargs = mock_create_app.call_args
        assert call_kwargs.kwargs["provider_names"] == ("local",)
        assert call_kwargs.kwargs["include_filters"] == ('state == "RUNNING"',)
        assert call_kwargs.kwargs["exclude_filters"] == ('name == "test"',)
