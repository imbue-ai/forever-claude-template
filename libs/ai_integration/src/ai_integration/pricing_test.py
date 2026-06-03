from ai_integration.data_types import Usage
from ai_integration.pricing import (
    counterfactual_direct_api_cost_usd,
    estimate_cost_usd,
    price_for,
)


def test_price_for_prefix_matching() -> None:
    haiku = price_for("claude-haiku-4-5")
    assert haiku is not None
    assert haiku.output_per_mtok == 5.0
    # Opus 4.1 is priced very differently from 4.5+, so prefixes must be specific.
    opus_41 = price_for("claude-opus-4-1")
    opus_45 = price_for("claude-opus-4-5")
    assert opus_41 is not None and opus_41.input_per_mtok == 15.0
    assert opus_45 is not None and opus_45.input_per_mtok == 5.0
    # A dated alias still matches its family prefix.
    dated = price_for("claude-haiku-4-5-20251001")
    assert dated is not None and dated.input_per_mtok == 1.0
    assert price_for("some-unknown-model") is None


def test_estimate_cost_usd_haiku() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    # Haiku 4.5: $1/MTok input + $5/MTok output.
    assert estimate_cost_usd("claude-haiku-4-5", usage) == 6.0


def test_estimate_cost_usd_unknown_model_is_none() -> None:
    assert estimate_cost_usd("nope", Usage(input_tokens=1, output_tokens=1)) is None


def test_counterfactual_is_positive_for_known_model() -> None:
    cost = counterfactual_direct_api_cost_usd(
        "claude-haiku-4-5", "x" * 4000, "y" * 4000
    )
    assert cost is not None
    assert cost > 0


def test_counterfactual_unknown_model_is_none() -> None:
    assert counterfactual_direct_api_cost_usd("nope", "prompt", "answer") is None
