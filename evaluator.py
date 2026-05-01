"""Strategy evaluator — the pure heart of the backtest tool.

The :func:`evaluate` function takes a betfairlightweight ``MarketBook`` and
the strategy portion of a plugin config, and returns a :class:`BetDecision`
(or list of them) describing the bet(s) the strategy wants to place — or a
:class:`NoBet` explaining why nothing was bet.

Critical properties:

* **Pure**. The function has no side effects: no I/O, no clock reads, no
  randomness. The same inputs always produce the same outputs.
* **Configuration-driven**. Every threshold, band, stake, and toggle is
  read from the supplied config. There are zero hardcoded strategy rules
  and zero string matches on rule names — rules are iterated as a list,
  matched mechanically by ``odds_band`` (and optional ``gap_lt`` /
  ``gap_gte``), and their ``name`` is treated as a label for reporting
  only. A plugin with rules called ``banana_1`` and ``banana_2`` runs
  through exactly the same code path as one with rules called
  ``rule_1`` and ``rule_2``.
* **Robust to missing data**. Missing prices, empty runner lists, in-play
  markets, and other degenerate inputs are turned into :class:`NoBet`
  results with descriptive reasons.
"""

from __future__ import annotations

from typing import Any, Iterable

from models.decisions import BetDecision, EvaluationResult, NoBet, Side
from models.schemas import StrategyConfig, StrategyRule


def evaluate(
    market_book: Any,
    strategy: StrategyConfig | dict[str, Any],
    *,
    point_value: float = 1.0,
    filters_country: Iterable[str] = (),
    filters_market_type: Iterable[str] = (),
) -> list[EvaluationResult]:
    """Evaluate a single :class:`MarketBook` against a strategy configuration.

    Args:
        market_book: A betfairlightweight ``MarketBook`` instance. Only the
            attributes documented in this module are accessed, so any
            duck-typed object with the same shape is acceptable.
        strategy: Either a :class:`StrategyConfig` or a raw mapping that
            validates against it. Accepting both keeps callers simple.
        point_value: Multiplier converting rule stake/base_stake into
            currency-denominated stake. Defaults to ``1.0``.
        filters_country: Optional country whitelist; if non-empty, markets
            outside the list are skipped.
        filters_market_type: Optional market type whitelist; if non-empty,
            other market types are skipped.

    Returns:
        A list of :class:`EvaluationResult`. Most rules produce a single
        result; rules with ``also_lay_2nd`` may produce two
        :class:`BetDecision` entries. A non-betting outcome is returned as
        a one-element list containing a :class:`NoBet`.
    """

    cfg = (
        strategy
        if isinstance(strategy, StrategyConfig)
        else StrategyConfig.model_validate(strategy)
    )

    md = getattr(market_book, "market_definition", None)
    if md is None:
        return [NoBet(reason="missing_price", detail="market_definition is missing")]

    if getattr(market_book, "inplay", False) or getattr(md, "in_play", False):
        return [NoBet(reason="in_play", detail="market has gone in-play")]

    country = getattr(md, "country_code", None)
    market_type = getattr(md, "market_type", None)
    if filters_country and country and country not in set(filters_country):
        return [
            NoBet(
                reason="filtered_country",
                detail=f"country {country!r} not in filter list",
            )
        ]
    if (
        filters_market_type
        and market_type
        and market_type not in set(filters_market_type)
    ):
        return [
            NoBet(
                reason="filtered_market_type",
                detail=f"market_type {market_type!r} not in filter list",
            )
        ]

    runners = _active_runners(market_book)
    if len(runners) < 1:
        return [
            NoBet(reason="no_active_runners", detail="no runners with a tradable price")
        ]

    runners.sort(key=lambda r: r.last_price_traded)
    favourite = runners[0]
    second = runners[1] if len(runners) > 1 else None
    fav_price: float = favourite.last_price_traded

    if fav_price < cfg.controls.hard_floor:
        return [
            NoBet(
                reason="blocked_by_floor",
                detail=f"favourite price {fav_price} below floor {cfg.controls.hard_floor}",
            )
        ]
    if fav_price > cfg.controls.hard_ceiling:
        return [
            NoBet(
                reason="blocked_by_ceiling",
                detail=f"favourite price {fav_price} above ceiling {cfg.controls.hard_ceiling}",
            )
        ]

    gap = (
        (second.last_price_traded - fav_price)
        if second is not None
        else float("inf")
    )

    rule = _select_rule(cfg.rules, fav_price, gap)
    if rule is None:
        return [
            NoBet(
                reason="no_matching_rule",
                detail=(
                    f"no rule matched favourite price {fav_price} (gap={gap})"
                ),
            )
        ]

    if cfg.controls.spread_control and second is not None:
        if gap < cfg.controls.jofs_spread:
            if not cfg.controls.jofs_enabled:
                return [
                    NoBet(
                        reason="spread_control_blocked",
                        detail=(
                            f"spread {gap:.3f} below jofs_spread {cfg.controls.jofs_spread}"
                        ),
                    )
                ]

    decisions: list[EvaluationResult] = []

    rule_stake = _resolve_stake(rule)
    uplift = float(cfg.controls.mark_uplift) if cfg.controls.mark_uplift else 1.0
    base_currency_stake = rule_stake * uplift * point_value

    split = (
        cfg.controls.jofs_enabled
        and second is not None
        and gap <= cfg.controls.jofs_spread
    )

    if split:
        per_leg_stake = base_currency_stake / 2.0
        decisions.append(
            _make_lay_decision(
                favourite, market_book, rule, fav_price, per_leg_stake
            )
        )
        decisions.append(
            _make_lay_decision(
                second,
                market_book,
                rule,
                second.last_price_traded,
                per_leg_stake,
                note="JOFS split leg",
            )
        )
    else:
        decisions.append(
            _make_lay_decision(
                favourite, market_book, rule, fav_price, base_currency_stake
            )
        )
        if rule.also_lay_2nd and second is not None:
            decisions.append(
                _make_lay_decision(
                    second,
                    market_book,
                    rule,
                    second.last_price_traded,
                    base_currency_stake,
                    note="rule.also_lay_2nd companion bet",
                )
            )

    return decisions


def _active_runners(market_book: Any) -> list[Any]:
    """Return runners with a tradable last_price_traded and ACTIVE status.

    The favourite calculation requires a price for sorting, so runners
    without a last_price_traded are excluded.
    """

    out: list[Any] = []
    for runner in getattr(market_book, "runners", []) or []:
        ltp = getattr(runner, "last_price_traded", None)
        status = getattr(runner, "status", None)
        if ltp is None or ltp <= 1.0:
            continue
        if status is not None and status != "ACTIVE":
            continue
        out.append(runner)
    return out


def _select_rule(
    rules: list[StrategyRule], price: float, gap: float
) -> StrategyRule | None:
    """Return the first rule whose odds_band contains ``price``.

    Within the band, ``gap_lt`` and ``gap_gte`` further narrow which rule
    applies — they are inclusive lower and exclusive upper bounds on the
    spread between favourite and 2nd favourite.
    """

    for rule in rules:
        lo, hi = rule.odds_band
        if not (lo <= price < hi):
            continue
        if rule.gap_lt is not None and not (gap < rule.gap_lt):
            continue
        if rule.gap_gte is not None and not (gap >= rule.gap_gte):
            continue
        return rule
    return None


def _resolve_stake(rule: StrategyRule) -> float:
    """Pull the stake value from a rule, preferring ``base_stake``."""

    if rule.base_stake is not None:
        return float(rule.base_stake)
    if rule.stake is not None:
        return float(rule.stake)
    raise ValueError(f"rule {rule.name!r} has no stake configured")


def _runner_name(market_book: Any, selection_id: int) -> str:
    """Resolve a runner's name from the market definition, falling back to ID."""

    md = getattr(market_book, "market_definition", None)
    if md is not None:
        for definition_runner in getattr(md, "runners", []) or []:
            if getattr(definition_runner, "selection_id", None) == selection_id:
                name = getattr(definition_runner, "name", None)
                if name:
                    return name
    return f"selection_{selection_id}"


def _make_lay_decision(
    runner: Any,
    market_book: Any,
    rule: StrategyRule,
    price: float,
    stake: float,
    note: str = "",
) -> BetDecision:
    """Construct a LAY :class:`BetDecision` for a runner."""

    selection_id = int(runner.selection_id)
    liability = round(stake * (price - 1.0), 2)
    return BetDecision(
        selection_id=selection_id,
        runner_name=_runner_name(market_book, selection_id),
        side=Side.LAY,
        price=round(float(price), 4),
        stake=round(float(stake), 2),
        liability=liability,
        rule_applied=rule.name,
        notes=note,
    )
