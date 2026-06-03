import pytest

from ai_integration.backends import parse_cli_result
from ai_integration.data_types import BillingPath
from ai_integration.errors import ClaudeCLIError


def test_parse_cli_result_extracts_text_usage_cost() -> None:
    data = {
        "result": "hi",
        "total_cost_usd": 0.01,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 2,
        },
    }
    result = parse_cli_result(data, "claude-haiku-4-5")
    assert result.text == "hi"
    assert result.billing_path is BillingPath.CLAUDE_CLI
    assert result.cost_usd == 0.01
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.cache_read_tokens == 2


def test_parse_cli_result_missing_cost_is_none() -> None:
    result = parse_cli_result({"result": "x"}, "claude-haiku-4-5")
    assert result.cost_usd is None
    assert result.text == "x"


def test_parse_cli_result_non_dict_raises() -> None:
    with pytest.raises(ClaudeCLIError):
        parse_cli_result(["not", "a", "dict"], "m")
