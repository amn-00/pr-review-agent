from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Usage:
    """What a run's lane calls cost. Field names match the review_runs columns."""

    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    cached_calls: int = 0
    tokens_saved: int = 0  # what the cached calls originally cost
    cost_usd: Decimal = Decimal(0)  # at list prices; we're on the free tier
