"""Per-token prices for cost logging. USD per 1M tokens, OpenAI standard tier, from
https://developers.openai.com/api/docs/pricing (checked 2026-10-18). Add a row when
you switch SLIDES_MODEL; unknown models are logged with no cost rather than a guess.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float


PRICES: dict[str, Price] = {
    "gpt-6.1-sol": Price(2.00, 0.10, 10.00),
    "gpt-6-sol": Price(2.00, 0.20, 10.00),
    "gpt-6-astra": Price(10.00, 1.00, 50.00),
    "gpt-6-luna": Price(0.10, 0.01, 0.50),
    "gpt-5.6-sol": Price(4.00, 0.40, 20.00),
    "gpt-5.6-terra": Price(2.00, 0.20, 12.00),
    "gpt-5.6-luna": Price(0.20, 0.02, 1.20),
    "gpt-5.5": Price(5.00, 0.50, 30.00),
    "gpt-5.4": Price(2.50, 0.25, 15.00),
    "gpt-5.4-mini": Price(0.75, 0.075, 4.50),
    "gpt-5.4-nano": Price(0.20, 0.02, 1.25),
    # Embeddings: input only.
    "text-embedding-3-large": Price(0.13, 0.13, 0.0),
    "text-embedding-3-small": Price(0.02, 0.02, 0.0),
}


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float | None:
    """Output tokens include reasoning tokens. Cached tokens are part of input tokens."""
    price = PRICES.get(model)
    if price is None:
        return None
    uncached = max(0, input_tokens - cached_tokens)
    return (
        uncached * price.input + cached_tokens * price.cached_input + output_tokens * price.output
    ) / 1_000_000
