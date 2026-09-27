"""Crude, tunable per-call cost estimates for harnesses without cost data."""

FALLBACK_COSTS = {
    "claude": 0.25,
    "codex": 0.15,
    "cursor": 0.10,
    "devin": 1.00,
}

DEFAULT_FALLBACK_COST = 0.10


def estimated_cost(harness: str) -> float:
    """Return the enforcement estimate for one successfully parsed call."""
    return FALLBACK_COSTS.get(harness, DEFAULT_FALLBACK_COST)
