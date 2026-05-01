"""Unit tests for :mod:`evaluator`.

The evaluator is a pure function — every behaviour can be verified with
duck-typed fixtures without touching network or filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluator import evaluate
from models.decisions import BetDecision, NoBet, Side
from models.schemas import StrategyConfig

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "mark_4rule_lay_v1.json"


@pytest.fixture
def mark_strategy() -> StrategyConfig:
    """Return the StrategyConfig from the bundled mark_4rule_lay_v1 plugin."""

    payload = json.loads(PLUGIN_PATH.read_text())
    return StrategyConfig.model_validate(payload["strategy"])


def test_rule_1_lays_favourite_in_lower_band(make_book, mark_strategy):
    book = make_book(favourite_price=1.80, second_price=4.00)
    results = evaluate(book, mark_strategy, point_value=7.5)
    assert len(results) == 1
    bet = results[0]
    assert isinstance(bet, BetDecision)
    assert bet.rule_applied == "rule_1"
    assert bet.side is Side.LAY
    assert bet.selection_id == 101
    assert bet.stake == pytest.approx(3 * 7.5)


def test_rule_2_lays_favourite_in_mid_band(make_book, mark_strategy):
    book = make_book(favourite_price=3.20, second_price=6.00)
    results = evaluate(book, mark_strategy, point_value=7.5)
    bet = results[0]
    assert isinstance(bet, BetDecision)
    assert bet.rule_applied == "rule_2"
    assert bet.stake == pytest.approx(2 * 7.5)


def test_rule_3a_doubles_with_companion_lay(make_book, mark_strategy):
    book = make_book(favourite_price=6.00, second_price=7.00)
    results = evaluate(book, mark_strategy, point_value=7.5)
    rules = {r.rule_applied for r in results if isinstance(r, BetDecision)}
    assert rules == {"rule_3a"}
    assert len(results) == 2
    selection_ids = {r.selection_id for r in results if isinstance(r, BetDecision)}
    assert selection_ids == {101, 102}


def test_rule_3b_no_companion_when_gap_large(make_book, mark_strategy):
    book = make_book(favourite_price=6.00, second_price=10.00)
    results = evaluate(book, mark_strategy, point_value=7.5)
    bets = [r for r in results if isinstance(r, BetDecision)]
    assert len(bets) == 1
    assert bets[0].rule_applied == "rule_3b"


def test_hard_floor_blocks_short_priced_favourite(make_book, mark_strategy):
    book = make_book(favourite_price=1.10, second_price=3.00)
    results = evaluate(book, mark_strategy, point_value=7.5)
    assert isinstance(results[0], NoBet)
    assert results[0].reason == "blocked_by_floor"


def test_hard_ceiling_blocks_long_priced_favourite(make_book, mark_strategy):
    book = make_book(favourite_price=12.00, second_price=20.00)
    results = evaluate(book, mark_strategy, point_value=7.5)
    assert isinstance(results[0], NoBet)
    assert results[0].reason == "blocked_by_ceiling"


def test_no_matching_rule_when_in_gap_band(make_book, mark_strategy):
    book = make_book(favourite_price=8.50, second_price=9.50)
    results = evaluate(book, mark_strategy, point_value=7.5)
    assert isinstance(results[0], NoBet)
    assert results[0].reason in {"no_matching_rule", "blocked_by_ceiling"}


def test_in_play_market_returns_no_bet(make_book, mark_strategy):
    book = make_book(favourite_price=2.00, second_price=4.00, in_play=True)
    results = evaluate(book, mark_strategy)
    assert isinstance(results[0], NoBet)
    assert results[0].reason == "in_play"


def test_country_filter_skips_unwanted_country(make_book, mark_strategy):
    book = make_book(favourite_price=2.00, second_price=4.00, country="FR")
    results = evaluate(book, mark_strategy, filters_country={"GB", "IE"})
    assert isinstance(results[0], NoBet)
    assert results[0].reason == "filtered_country"


def test_market_type_filter_skips_unwanted_type(make_book, mark_strategy):
    book = make_book(favourite_price=2.00, second_price=4.00, market_type="PLACE")
    results = evaluate(book, mark_strategy, filters_market_type={"WIN"})
    assert isinstance(results[0], NoBet)
    assert results[0].reason == "filtered_market_type"


def test_jofs_split_when_first_and_second_close(make_book):
    """Joint odds favourite splitting halves the stake across two bets."""

    strategy = StrategyConfig.model_validate(
        {
            "rules": [
                {"name": "band_a", "odds_band": [2.0, 5.0], "base_stake": 4}
            ],
            "controls": {
                "hard_floor": 1.5,
                "hard_ceiling": 10.0,
                "jofs_enabled": True,
                "jofs_spread": 0.30,
            },
        }
    )
    book = make_book(favourite_price=3.00, second_price=3.10)
    results = evaluate(book, strategy, point_value=10.0)
    assert len(results) == 2
    assert all(isinstance(r, BetDecision) for r in results)
    stakes = sorted(r.stake for r in results if isinstance(r, BetDecision))
    assert stakes == pytest.approx([20.0, 20.0])


def test_spread_control_blocks_when_jofs_disabled(make_book):
    """spread_control + jofs disabled rejects markets with tight spread."""

    strategy = StrategyConfig.model_validate(
        {
            "rules": [
                {"name": "band_a", "odds_band": [2.0, 5.0], "base_stake": 2}
            ],
            "controls": {
                "hard_floor": 1.5,
                "hard_ceiling": 10.0,
                "jofs_enabled": False,
                "jofs_spread": 0.30,
                "spread_control": True,
            },
        }
    )
    book = make_book(favourite_price=3.00, second_price=3.10)
    results = evaluate(book, strategy)
    assert isinstance(results[0], NoBet)
    assert results[0].reason == "spread_control_blocked"


def test_evaluator_is_pure(make_book, mark_strategy):
    """Calling evaluate twice with the same input must return equal output."""

    book = make_book(favourite_price=2.50, second_price=5.00)
    a = evaluate(book, mark_strategy, point_value=7.5)
    b = evaluate(book, mark_strategy, point_value=7.5)
    assert a == b


def test_no_active_runners_short_circuits(make_book, mark_strategy):
    book = make_book(favourite_price=2.5, favourite_status="REMOVED")
    book.runners[0].last_price_traded = None
    results = evaluate(book, mark_strategy)
    assert isinstance(results[0], NoBet)
    assert results[0].reason == "no_active_runners"


def test_runner_name_is_resolved_from_market_definition(make_book, mark_strategy):
    book = make_book(favourite_price=1.80, second_price=4.00)
    bet = evaluate(book, mark_strategy)[0]
    assert isinstance(bet, BetDecision)
    assert bet.runner_name == "Alpha"


def test_mark_uplift_multiplies_stake(make_book):
    """``mark_uplift`` is a stake multiplier: stake = base_stake * uplift * point."""

    strategy = StrategyConfig.model_validate(
        {
            "rules": [
                {"name": "band_a", "odds_band": [2.0, 5.0], "base_stake": 4}
            ],
            "controls": {
                "hard_floor": 1.5,
                "hard_ceiling": 10.0,
                "mark_uplift": 2.0,
            },
        }
    )
    book = make_book(favourite_price=3.00, second_price=6.00)
    bet = evaluate(book, strategy, point_value=10.0)[0]
    assert isinstance(bet, BetDecision)
    assert bet.price == pytest.approx(3.00)
    assert bet.stake == pytest.approx(4 * 2.0 * 10.0)


def test_mark_uplift_unset_means_unit_multiplier(make_book):
    """When mark_uplift is omitted the stake is base_stake * point_value."""

    strategy = StrategyConfig.model_validate(
        {
            "rules": [
                {"name": "band_a", "odds_band": [2.0, 5.0], "base_stake": 3}
            ],
            "controls": {"hard_floor": 1.5, "hard_ceiling": 10.0},
        }
    )
    book = make_book(favourite_price=3.00, second_price=6.00)
    bet = evaluate(book, strategy, point_value=5.0)[0]
    assert isinstance(bet, BetDecision)
    assert bet.stake == pytest.approx(3 * 5.0)


def test_evaluator_treats_arbitrary_rule_names_identically(make_book):
    """Generics check: rule names are labels, not logic branches."""

    strategy = StrategyConfig.model_validate(
        {
            "rules": [
                {"name": "banana_1", "odds_band": [10.0, 50.0], "base_stake": 7},
                {"name": "banana_2", "odds_band": [50.0, 1000.0], "base_stake": 9},
            ],
            "controls": {
                "hard_floor": 1.0,
                "hard_ceiling": 1000.0,
                "magic_factor": 1.5,
            },
        }
    )
    book = make_book(favourite_price=15.0, second_price=80.0)
    bet = evaluate(book, strategy, point_value=2.0)[0]
    assert isinstance(bet, BetDecision)
    assert bet.rule_applied == "banana_1"
    assert bet.stake == pytest.approx(7 * 2.0)
    book2 = make_book(favourite_price=80.0, second_price=200.0)
    bet2 = evaluate(book2, strategy, point_value=2.0)[0]
    assert isinstance(bet2, BetDecision)
    assert bet2.rule_applied == "banana_2"
    assert bet2.stake == pytest.approx(9 * 2.0)


def test_unknown_controls_are_accepted_and_ignored(make_book):
    """Plugins can introduce new controls without code changes — they're allowed but not applied."""

    strategy = StrategyConfig.model_validate(
        {
            "rules": [
                {"name": "anything", "odds_band": [1.5, 10.0], "base_stake": 1}
            ],
            "controls": {
                "hard_floor": 1.0,
                "hard_ceiling": 100.0,
                "future_control_x": "experimental",
                "future_control_y": [1, 2, 3],
            },
        }
    )
    book = make_book(favourite_price=2.50, second_price=5.0)
    results = evaluate(book, strategy, point_value=1.0)
    assert isinstance(results[0], BetDecision)
