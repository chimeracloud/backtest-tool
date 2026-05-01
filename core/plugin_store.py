"""Discovery and validation of strategy plugins from the plugins directory.

Plugins are JSON documents that conform to :class:`PluginConfig`. Each one
sits in ``plugins/<name>.json`` and is loaded eagerly at startup; new
plugins added at runtime are picked up by re-calling
:meth:`PluginStore.refresh`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.config import get_settings
from core.logging import get_logger
from models.schemas import (
    PluginConfig,
    PluginInfo,
    PluginRuleSchema,
    PluginSchema,
)

logger = get_logger(__name__)


class PluginNotFoundError(LookupError):
    """Raised when a plugin name does not match any installed plugin."""


class PluginLoadError(RuntimeError):
    """Raised when a plugin file fails validation."""


class PluginStore:
    """In-memory registry of installed strategy plugins."""

    def __init__(self, plugins_dir: Path) -> None:
        self._dir = plugins_dir
        self._plugins: dict[str, PluginConfig] = {}
        self.refresh()

    def refresh(self) -> None:
        """Re-scan the plugins directory and rebuild the registry."""

        if not self._dir.exists():
            logger.warning("plugins directory missing", extra={"path": str(self._dir)})
            self._plugins = {}
            return

        loaded: dict[str, PluginConfig] = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                plugin = PluginConfig.model_validate(data)
            except json.JSONDecodeError as exc:
                logger.error(
                    "plugin file is not valid JSON",
                    extra={"path": str(path), "error": str(exc)},
                )
                continue
            except ValidationError as exc:
                logger.error(
                    "plugin file failed schema validation",
                    extra={"path": str(path), "errors": exc.errors()},
                )
                continue
            loaded[plugin.name] = plugin
        self._plugins = loaded
        logger.info("plugins loaded", extra={"count": len(loaded)})

    def list(self) -> list[PluginInfo]:
        """Return a list of installed plugins for the GUI listing endpoint."""

        return [
            PluginInfo(
                name=p.name,
                version=p.version,
                description=p.description,
                rule_count=len(p.strategy.rules),
            )
            for p in self._plugins.values()
        ]

    def get(self, name: str) -> PluginConfig:
        """Return the full plugin config for ``name``.

        Raises:
            PluginNotFoundError: if no plugin by that name is installed.
        """

        plugin = self._plugins.get(name)
        if plugin is None:
            raise PluginNotFoundError(name)
        return plugin

    def schema_for(self, name: str) -> PluginSchema:
        """Return the editor schema served by GET /api/plugins/{name}/schema.

        The response carries: a description of every configurable field
        (type, default, min/max, enum) and the plugin's current values
        under ``defaults`` so the portal can render a pre-populated form.
        """

        plugin = self.get(name)
        return PluginSchema(
            name=plugin.name,
            version=plugin.version,
            description=plugin.description,
            source_fields=_SOURCE_FIELDS,
            parser_fields=_PARSER_FIELDS,
            rule_fields=_RULE_FIELDS,
            control_fields=_CONTROL_FIELDS,
            staking_fields=_STAKING_FIELDS,
            defaults=plugin.model_dump(mode="json"),
        )


_SOURCE_FIELDS: list[PluginRuleSchema] = [
    PluginRuleSchema(
        name="type",
        type="string",
        required=True,
        enum=["gcs", "betfair_historic"],
        default="gcs",
        description=(
            "Data source mode. 'gcs' reads pre-downloaded .bz2 files from a "
            "Cloud Storage bucket. 'betfair_historic' downloads on-demand "
            "from the Betfair Historic Data API."
        ),
    ),
    PluginRuleSchema(
        name="bucket",
        type="string",
        required=False,
        description=(
            "GCS path including any prefix, e.g. gs://bucket/PATH/. "
            "Required when type='gcs'."
        ),
    ),
    PluginRuleSchema(
        name="date_range.start",
        type="string<date>",
        required=True,
        description="Inclusive start date (YYYY-MM-DD).",
    ),
    PluginRuleSchema(
        name="date_range.end",
        type="string<date>",
        required=True,
        description="Inclusive end date (YYYY-MM-DD).",
    ),
    PluginRuleSchema(
        name="filters.countries",
        type="array<string>",
        required=False,
        default=[],
        description="ISO country codes to include (e.g. ['GB', 'IE']).",
    ),
    PluginRuleSchema(
        name="filters.market_types",
        type="array<string>",
        required=False,
        default=[],
        description="Betfair market types to include (e.g. ['WIN', 'PLACE']).",
    ),
    PluginRuleSchema(
        name="filters.sport",
        type="string",
        required=False,
        default="Horse Racing",
        description="Sport name (used by the Betfair Historic API).",
    ),
    PluginRuleSchema(
        name="filters.plan",
        type="string",
        required=False,
        enum=["Basic Plan", "Advanced Plan", "Pro Plan"],
        default="Basic Plan",
        description="Historic Data API plan to download from.",
    ),
    PluginRuleSchema(
        name="filters.file_types",
        type="array<string>",
        required=False,
        enum=["M", "E", "I"],
        default=["M"],
        description=(
            "File types: M = market change messages, E = events, I = items."
        ),
    ),
    PluginRuleSchema(
        name="persist_to_bucket",
        type="string",
        required=False,
        description=(
            "Optional gs://... destination. When set, downloaded historic "
            "files are mirrored back to GCS so future runs can use the "
            "'gcs' source mode."
        ),
    ),
]


_PARSER_FIELDS: list[PluginRuleSchema] = [
    PluginRuleSchema(
        name="format",
        type="string",
        required=True,
        enum=["betfair_mcm"],
        default="betfair_mcm",
        description="Source data format.",
    ),
    PluginRuleSchema(
        name="time_before_off_seconds",
        type="integer",
        required=True,
        default=300,
        minimum=0,
        maximum=86_400,
        description="Seconds before market off-time at which to evaluate.",
    ),
    PluginRuleSchema(
        name="price_field",
        type="string",
        required=True,
        enum=["ltp", "back", "lay"],
        default="ltp",
        description="Price field used to identify the favourite.",
    ),
    PluginRuleSchema(
        name="extract_bsp",
        type="boolean",
        required=False,
        default=True,
        description="Settle bets using actual BSP when available.",
    ),
]


_RULE_FIELDS: list[PluginRuleSchema] = [
    PluginRuleSchema(name="name", type="string", required=True),
    PluginRuleSchema(
        name="odds_band",
        type="array<float>",
        required=True,
        description="Two-element [lower, upper] odds bound.",
    ),
    PluginRuleSchema(name="base_stake", type="number", required=False, minimum=0),
    PluginRuleSchema(name="stake", type="number", required=False, minimum=0),
    PluginRuleSchema(name="gap_lt", type="number", required=False, minimum=0),
    PluginRuleSchema(name="gap_gte", type="number", required=False, minimum=0),
    PluginRuleSchema(
        name="also_lay_2nd",
        type="boolean",
        required=False,
        default=False,
        description="When true, the rule places matching lays on the 2nd favourite.",
    ),
]


_CONTROL_FIELDS: list[PluginRuleSchema] = [
    PluginRuleSchema(
        name="hard_floor",
        type="number",
        required=True,
        default=1.01,
        minimum=1.0,
        description="Reject markets where favourite price is below this.",
    ),
    PluginRuleSchema(
        name="hard_ceiling",
        type="number",
        required=True,
        default=1000.0,
        minimum=1.0,
        description="Reject markets where favourite price is above this.",
    ),
    PluginRuleSchema(
        name="jofs_enabled",
        type="boolean",
        required=False,
        default=False,
        description="Enable Joint Odds Favourite Splitting.",
    ),
    PluginRuleSchema(
        name="jofs_spread",
        type="number",
        required=False,
        default=0.20,
        minimum=0.0,
        description="Maximum gap (in price units) considered a joint favourite.",
    ),
    PluginRuleSchema(
        name="mark_uplift",
        type="number",
        required=False,
        minimum=0,
        description=(
            "Stake multiplier applied to the matched rule's base_stake. "
            "When set, final_stake = base_stake * mark_uplift * point_value."
        ),
    ),
    PluginRuleSchema(
        name="spread_control",
        type="boolean",
        required=False,
        default=False,
        description="Block markets when 1st/2nd favourite spread is too tight.",
    ),
]


_STAKING_FIELDS: list[PluginRuleSchema] = [
    PluginRuleSchema(
        name="point_value",
        type="number",
        required=True,
        default=1.0,
        minimum=0.0,
        description="Multiplier converting points (rule stakes) into currency.",
    ),
]


_STORE: PluginStore | None = None


def get_plugin_store() -> PluginStore:
    """Return the lazily-initialised process-wide :class:`PluginStore`."""

    global _STORE
    if _STORE is None:
        _STORE = PluginStore(get_settings().plugins_dir)
    return _STORE


def reset_plugin_store(plugins_dir: Path | None = None) -> PluginStore:
    """Force a refresh, optionally pointing at a different directory.

    Used by unit tests to load fixtures.
    """

    global _STORE
    target = plugins_dir or get_settings().plugins_dir
    _STORE = PluginStore(target)
    return _STORE
