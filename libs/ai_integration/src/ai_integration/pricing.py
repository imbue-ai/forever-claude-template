"""Per-model Anthropic API price table and cost / savings estimation.

Prices are USD per million tokens (MTok), first-party Claude API list prices
confirmed against https://platform.claude.com/docs/en/about-claude/pricing on
2026-06-02. They change over time -- treat this table as the single place to
update, and keep it in sync with the docs.

The same table powers both the spend tracker's cost estimation and the keyless
``claude -p`` savings nudge (the counterfactual "what would the direct API have
cost").
"""

from imbue.imbue_common.frozen_model import FrozenModel

from ai_integration.data_types import Usage

# Default model for one-shot completions: the cheapest current tier. Callers can
# override per call.
DEFAULT_MODEL = "claude-haiku-4-5"

_PER_MTOK = 1_000_000.0


class ModelPrice(FrozenModel):
    """USD-per-MTok prices for one model."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_5m_per_mtok: float


# Keyed by model-id prefix, longest-match-wins. Opus 4.5+ is priced very
# differently from the older 4.1, so the table keys on specific minor versions
# rather than a broad ``claude-opus-4`` prefix.
_PRICE_TABLE: dict[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice(
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_read_per_mtok=0.5,
        cache_write_5m_per_mtok=6.25,
    ),
    "claude-opus-4-7": ModelPrice(
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_read_per_mtok=0.5,
        cache_write_5m_per_mtok=6.25,
    ),
    "claude-opus-4-6": ModelPrice(
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_read_per_mtok=0.5,
        cache_write_5m_per_mtok=6.25,
    ),
    "claude-opus-4-5": ModelPrice(
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_read_per_mtok=0.5,
        cache_write_5m_per_mtok=6.25,
    ),
    "claude-opus-4-1": ModelPrice(
        input_per_mtok=15.0,
        output_per_mtok=75.0,
        cache_read_per_mtok=1.5,
        cache_write_5m_per_mtok=18.75,
    ),
    "claude-sonnet-4-6": ModelPrice(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.3,
        cache_write_5m_per_mtok=3.75,
    ),
    "claude-sonnet-4-5": ModelPrice(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.3,
        cache_write_5m_per_mtok=3.75,
    ),
    "claude-haiku-4-5": ModelPrice(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_read_per_mtok=0.1,
        cache_write_5m_per_mtok=1.25,
    ),
}


def price_for(model: str) -> ModelPrice | None:
    """Return the price entry for ``model`` (longest-prefix match), or ``None``.

    ``None`` means the model is unknown to the table; callers should skip cost /
    savings reporting rather than guess.
    """
    best: tuple[int, ModelPrice] | None = None
    for prefix, price in _PRICE_TABLE.items():
        if model.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price)
    return best[1] if best is not None else None


def estimate_cost_usd(model: str, usage: Usage) -> float | None:
    """Estimate the direct-API cost of ``usage`` on ``model``, or ``None``.

    Uncached input is billed at the base input rate, cache reads at the cache-read
    rate, cache writes at the 5-minute write rate, and output at the output rate.
    """
    price = price_for(model)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input_per_mtok
        + usage.cache_read_tokens * price.cache_read_per_mtok
        + usage.cache_write_tokens * price.cache_write_5m_per_mtok
        + usage.output_tokens * price.output_per_mtok
    ) / _PER_MTOK


def estimate_tokens(text: str) -> int:
    """Rough token estimate for a text blob (~4 chars/token).

    Used only for the keyless savings counterfactual, where we know the user's
    prompt/response text but not an exact tokenizer count. Deliberately
    approximate; the nudge is labelled as an estimate.
    """
    return max(1, (len(text) + 3) // 4)


def counterfactual_direct_api_cost_usd(
    model: str, prompt: str, completion: str
) -> float | None:
    """Estimate what a direct-API call for this prompt/response would have cost.

    This is the "if you had a key" figure for the keyless ``claude -p`` savings
    nudge: it prices only the user's prompt + the model's response (a direct API
    call carries none of ``claude -p``'s ~127k-token agent-context overhead).
    Returns ``None`` for unknown models.
    """
    price = price_for(model)
    if price is None:
        return None
    usage = Usage(
        input_tokens=estimate_tokens(prompt),
        output_tokens=estimate_tokens(completion),
    )
    return estimate_cost_usd(model, usage)
