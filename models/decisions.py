"""Outputs produced by the strategy evaluator.

The evaluator is a pure function. For each market it returns either a
:class:`BetDecision` describing a bet to place, or a :class:`NoBet` carrying
the reason the market was skipped. Both are immutable, frozen dataclasses
so callers cannot mutate the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class Side(str, Enum):
    """Side of a Betfair bet."""

    LAY = "LAY"
    BACK = "BACK"


@dataclass(frozen=True, slots=True)
class BetDecision:
    """A single bet the strategy wants to place.

    Attributes:
        selection_id: Betfair selection id of the runner being bet on.
        runner_name: Human-readable runner name (from market definition).
        side: ``LAY`` or ``BACK``.
        price: The price (decimal odds) at which the bet is placed.
        stake: Stake size in account currency units.
        liability: Liability if the bet loses (stake * (price - 1) for lay).
        rule_applied: Name of the strategy rule that produced this decision.
        notes: Optional free-text annotation for traceability.
    """

    selection_id: int
    runner_name: str
    side: Side
    price: float
    stake: float
    liability: float
    rule_applied: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class NoBet:
    """Returned when the strategy declines to bet on a market.

    Attributes:
        reason: Short, machine-readable code describing why no bet was made.
        detail: Human-readable explanation suitable for logging.
    """

    reason: Literal[
        "no_active_runners",
        "blocked_by_floor",
        "blocked_by_ceiling",
        "no_matching_rule",
        "in_play",
        "missing_price",
        "filtered_market_type",
        "filtered_country",
        "spread_control_blocked",
    ]
    detail: str = ""


EvaluationResult = BetDecision | NoBet
