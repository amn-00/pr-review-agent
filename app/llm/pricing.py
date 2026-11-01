"""Groq list prices, for a hypothetical cost per run (we run on the free tier, so it's $0).

USD per million tokens, (input, output), on-demand. Checked 2026-11-01; sources disagreed, these
are the most consistent recent figures (e.g. https://www.morphllm.com/groq-pricing citing
Groq's docs, October 2026). Groq's console has the authoritative numbers.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

PRICES_PER_MILLION = {
    "openai/gpt-oss-120b": (Decimal("0.15"), Decimal("0.60")),
    "openai/gpt-oss-20b": (Decimal("0.075"), Decimal("0.30")),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Cost at list prices. An unknown model costs 0 (with a warning) rather than failing the
    review over a reporting number."""
    if model not in PRICES_PER_MILLION:
        logger.warning("no list price for model %s; counting its cost as 0", model)
        return Decimal(0)
    price_in, price_out = PRICES_PER_MILLION[model]
    return (price_in * tokens_in + price_out * tokens_out) / 1_000_000
