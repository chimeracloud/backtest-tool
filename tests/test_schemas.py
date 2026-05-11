"""Validation tests for the request/response schemas.

These check that the contract documented in the README is enforced by
Pydantic — a typo in a strategy field returns 422 long before the worker
attempts to run it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.schemas import (
    BacktestRequest,
    GcsSourceConfig,
    PluginConfig,
    SourceType,
    StrategyConfig,
    StrategyControls,
    StrategyRule,
)


def _valid_request_payload() -> dict:
    return {
        "plugin": {
            "name": "mark_4rule_lay_v1",
            "version": "1.0.0",
            "source": {
                "type": "gcs",
                "bucket": "gs://betfair-basic-historic/ADVANCED/",
                "date_range": {"start": "2025-01-01", "end": "2025-01-31"},
                "filters": {
                    "countries": ["GB", "IE"],
                    "market_types": ["WIN"],
                },
            },
            "parser": {
                "format": "betfair_mcm",
                "time_before_off_seconds": 300,
                "price_field": "ltp",
                "extract_bsp": True,
            },
            "strategy": {
                "rules": [
                    {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 3}
                ],
                "controls": {"hard_floor": 1.5, "hard_ceiling": 8.0},
            },
            "staking": {"point_value": 7.5},
        },
    }


def test_full_request_validates() -> None:
    request = BacktestRequest.model_validate(_valid_request_payload())
    assert isinstance(request.plugin.source, GcsSourceConfig)
    assert request.plugin.source.type is SourceType.GCS
    assert len(request.plugin.strategy.rules) == 1


def test_unknown_top_level_field_is_rejected() -> None:
    payload = _valid_request_payload()
    payload["unexpected"] = "value"
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(payload)


def test_rule_without_stake_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyRule.model_validate({"name": "x", "odds_band": [1.5, 2.0]})


def test_floor_must_be_below_ceiling() -> None:
    with pytest.raises(ValidationError):
        StrategyControls.model_validate(
            {"hard_floor": 5.0, "hard_ceiling": 4.0}
        )


def test_date_range_must_be_ordered() -> None:
    payload = _valid_request_payload()
    payload["plugin"]["source"]["date_range"] = {
        "start": "2025-02-01",
        "end": "2025-01-01",
    }
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(payload)


def test_betfair_historic_source_round_trips() -> None:
    payload = _valid_request_payload()
    payload["plugin"]["source"] = {
        "type": "betfair_historic",
        "date_range": {"start": "2025-01-01", "end": "2025-01-31"},
        "filters": {
            "countries": ["GB"],
            "market_types": ["WIN"],
            "sport": "Horse Racing",
            "plan": "Basic Plan",
        },
        "persist_to_bucket": "gs://betfair-basic-historic/MIRROR/",
    }
    request = BacktestRequest.model_validate(payload)
    assert request.plugin.source.type is SourceType.BETFAIR_HISTORIC


def test_strategy_requires_at_least_one_rule() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate({"rules": []})


def test_plugin_round_trips_to_json_and_back() -> None:
    plugin = PluginConfig.model_validate(_valid_request_payload()["plugin"])
    serialised = plugin.model_dump_json()
    rehydrated = PluginConfig.model_validate_json(serialised)
    assert rehydrated == plugin


def test_strategy_controls_accept_unknown_fields() -> None:
    """Plugins can add new controls without breaking schema validation."""

    controls = StrategyControls.model_validate(
        {
            "hard_floor": 1.5,
            "hard_ceiling": 10.0,
            "magic_factor": 1.5,
            "vendor_specific_flag": True,
        }
    )
    assert controls.hard_floor == 1.5
    assert getattr(controls, "magic_factor", None) == 1.5


def test_request_without_plugin_source_uses_top_level_date_range() -> None:
    """New shape: plugin omits source; date_range + filters travel on the request."""

    payload = {
        "plugin": {
            "name": "mark_4rule_lay_v1",
            "version": "1.0.0",
            "parser": {
                "format": "betfair_mcm",
                "time_before_off_seconds": 300,
                "price_field": "ltp",
                "extract_bsp": True,
            },
            "strategy": {
                "rules": [
                    {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 3}
                ],
                "controls": {"hard_floor": 1.5, "hard_ceiling": 8.0},
            },
            "staking": {"point_value": 7.5},
        },
        "date_range": {"start": "2026-01-01", "end": "2026-01-31"},
        "filters": {"countries": ["GB", "IE"], "market_types": ["WIN"]},
    }
    request = BacktestRequest.model_validate(payload)
    assert request.plugin.source is None
    assert request.date_range is not None
    assert request.date_range.start.isoformat() == "2026-01-01"
    assert request.filters is not None
    assert request.filters.countries == ["GB", "IE"]


def test_request_with_only_plugin_block_is_accepted() -> None:
    """Plugin source / request date_range are both optional at the schema level.

    The service layer raises a ValueError when neither is present (see
    BacktestService._resolve_source); that's a runtime contract, not a
    schema-level one, so the schema accepts the bare plugin payload.
    """

    payload = {
        "plugin": {
            "name": "mark_4rule_lay_v1",
            "version": "1.0.0",
            "parser": {
                "format": "betfair_mcm",
                "time_before_off_seconds": 300,
                "price_field": "ltp",
                "extract_bsp": True,
            },
            "strategy": {
                "rules": [
                    {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 3}
                ],
                "controls": {"hard_floor": 1.5, "hard_ceiling": 8.0},
            },
            "staking": {"point_value": 7.5},
        },
    }
    request = BacktestRequest.model_validate(payload)
    assert request.plugin.source is None
    assert request.date_range is None
    assert request.source is None
