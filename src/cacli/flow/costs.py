"""Crude, tunable per-call cost estimates for providers without cost data."""

FALLBACK_COSTS = {
    "claude": 0.25,
    "codex": 0.15,
    "gemini": 0.05,
    "cursor": 0.10,
    "devin": 1.00,
}

DEFAULT_FALLBACK_COST = 0.10


def estimated_cost(provider: str) -> float:
    """Return the enforcement estimate for one successfully parsed call."""
    return FALLBACK_COSTS.get(provider, DEFAULT_FALLBACK_COST)
