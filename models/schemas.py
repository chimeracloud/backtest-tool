"""Pydantic schemas for every public request and response payload.

Every shape that crosses the API boundary lives here so validation is
centralised and the OpenAPI document is complete. All models forbid
unknown fields so silent typos in user-supplied JSON fail fast.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    """Base model that rejects unknown fields and trims strings."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class JobStatus(str, Enum):
    """Lifecycle states for a backtest job."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SourceType(str, Enum):
    """Where to read historic data from."""

    GCS = "gcs"
    BETFAIR_HISTORIC = "betfair_historic"


class FileType(str, Enum):
    """Betfair Historic Data API file types."""

    MARKET = "M"
    EVENT = "E"
    ITEMS = "I"


class DateRange(_Strict):
    """Inclusive date range used to scope source data."""

    start: date
    end: date

    @model_validator(mode="after")
    def _check_order(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("date_range.end must be on or after date_range.start")
        return self


class SourceFilters(_Strict):
    """Filters applied when listing or downloading historic files."""

    countries: list[str] = Field(
        default_factory=list,
        description="ISO country codes (e.g. ['GB', 'IE']).",
    )
    market_types: list[str] = Field(
        default_factory=list,
        description="Betfair market types (e.g. ['WIN', 'PLACE']).",
    )
    sport: str = Field(
        default="Horse Racing",
        description="Betfair sport name (used by the historic download API).",
    )
    plan: Literal["Basic Plan", "Advanced Plan", "Pro Plan"] = Field(
        default="Basic Plan",
        description="Historic Data API plan to download from.",
    )
    file_types: list[FileType] = Field(
        default_factory=lambda: [FileType.MARKET],
        description="Historic Data API file types to include.",
    )


class GcsSourceConfig(_Strict):
    """Read pre-downloaded .bz2 files from a GCS bucket."""

    type: Literal[SourceType.GCS] = SourceType.GCS
    bucket: str = Field(
        ...,
        description="GCS path including any prefix, e.g. gs://bucket/PATH/",
    )
    date_range: DateRange
    filters: SourceFilters = Field(default_factory=SourceFilters)


class BetfairHistoricSourceConfig(_Strict):
    """Download files on-demand via the Betfair Historic Data API."""

    type: Literal[SourceType.BETFAIR_HISTORIC] = SourceType.BETFAIR_HISTORIC
    date_range: DateRange
    filters: SourceFilters = Field(default_factory=SourceFilters)
    persist_to_bucket: str | None = Field(
        default=None,
        description=(
            "Optional gs://... path. If set, downloaded files are uploaded so "
            "future runs can use the GCS source mode."
        ),
    )


SourceConfig = Annotated[
    GcsSourceConfig | BetfairHistoricSourceConfig,
    Field(discriminator="type"),
]


class ParserConfig(_Strict):
    """Controls how raw market updates are converted into evaluator input."""

    format: Literal["betfair_mcm"] = "betfair_mcm"
    time_before_off_seconds: int = Field(
        default=300,
        ge=0,
        le=86_400,
        description="Seconds before market_time at which to evaluate.",
    )
    price_field: Literal["ltp", "back", "lay"] = Field(
        default="ltp",
        description="Price field used to identify the favourite.",
    )
    extract_bsp: bool = Field(
        default=True,
        description="Settle bets using SP (actual_sp) when available.",
    )


class StrategyRule(_Strict):
    """One element of the strategy.rules array.

    The evaluator iterates rules in order and applies the first whose
    odds_band contains the favourite price. ``extra="allow"`` is the only
    relaxation from :class:`_Strict` — rules accept additional, plugin-
    specific fields so future strategies can encode bespoke parameters
    without changing this schema.
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    name: str
    odds_band: tuple[float, float] = Field(
        ...,
        description="Inclusive lower, exclusive upper odds bound.",
    )
    base_stake: float | None = Field(default=None, ge=0)
    stake: float | None = Field(default=None, ge=0)
    gap_lt: float | None = Field(default=None, ge=0)
    gap_gte: float | None = Field(default=None, ge=0)
    also_lay_2nd: bool = False

    @model_validator(mode="after")
    def _has_stake(self) -> "StrategyRule":
        if self.base_stake is None and self.stake is None:
            raise ValueError(
                f"rule '{self.name}' must define either base_stake or stake"
            )
        return self


class StrategyControls(BaseModel):
    """Cross-rule guards and modifiers.

    ``extra="allow"`` lets plugins introduce new controls without code
    changes — the evaluator applies the controls it understands and
    silently passes the rest through to the result document for
    auditability.
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    hard_floor: float = Field(default=1.01, ge=1.0)
    hard_ceiling: float = Field(default=1000.0, gt=1.0)
    jofs_enabled: bool = False
    jofs_spread: float = Field(default=0.20, ge=0.0)
    mark_uplift: float | None = Field(default=None, ge=0.0)
    spread_control: bool = False

    @model_validator(mode="after")
    def _check_floor_ceiling(self) -> "StrategyControls":
        if self.hard_ceiling <= self.hard_floor:
            raise ValueError("hard_ceiling must be greater than hard_floor")
        return self


class StrategyConfig(_Strict):
    """The decisive part of a plugin: what to bet and when."""

    rules: list[StrategyRule] = Field(..., min_length=1)
    controls: StrategyControls = Field(default_factory=StrategyControls)


class StakingConfig(_Strict):
    """How stake numbers in rules translate to currency."""

    point_value: float = Field(
        default=1.0,
        gt=0,
        description="Multiplier applied to base_stake / stake to get currency.",
    )


class PluginConfig(_Strict):
    """Plugin payload received with each backtest request.

    A plugin describes the strategy: how to parse market updates, what to
    do with them, and how to stake. Source location (GCS bucket or
    Betfair Historic API) is intentionally NOT a plugin concern — it is
    a runtime choice carried on the :class:`BacktestRequest` (or, when
    unset there, resolved from the service-level admin config).

    ``source`` is accepted for backward compatibility with older plugin
    JSON files that pinned a bucket inline. When present it is honoured;
    otherwise the request-level source applies.
    """

    name: str
    version: str
    description: str | None = None
    source: SourceConfig | None = None
    parser: ParserConfig = Field(default_factory=ParserConfig)
    strategy: StrategyConfig
    staking: StakingConfig = Field(default_factory=StakingConfig)


class BacktestRequest(_Strict):
    """Body of POST /api/backtest.

    Source resolution order (first wins):

    1. ``plugin.source`` (legacy inline source; deprecated for new plugins)
    2. This request's ``source`` (explicit override)
    3. The service-level default GCS bucket from :class:`AdminConfig`,
       combined with this request's ``date_range`` and ``filters``.
    """

    plugin: PluginConfig
    date_range: DateRange | None = Field(
        default=None,
        description=(
            "Date window for the backtest. Required unless plugin.source "
            "or this request's source already carries a date_range."
        ),
    )
    filters: SourceFilters | None = Field(
        default=None,
        description=(
            "Filters (countries, market_types, sport, plan, file_types) "
            "applied to the chosen source. If absent, defaults to an empty "
            "filter set (i.e. include everything)."
        ),
    )
    source: SourceConfig | None = Field(
        default=None,
        description=(
            "Optional explicit source override at request time. If neither "
            "this nor plugin.source is supplied, a GCS source is built from "
            "the admin default_source_bucket."
        ),
    )


class BacktestSubmission(_Strict):
    """Synchronous response from POST /api/backtest."""

    job_id: str
    status: JobStatus
    submitted_at: datetime
    plugin: str


class JobProgress(_Strict):
    """Progress tracking for a running job."""

    files_total: int = 0
    files_processed: int = 0
    markets_total: int = 0
    markets_processed: int = 0
    bets_recorded: int = 0
    percentage: float = Field(default=0.0, ge=0.0, le=100.0)


class BacktestSummary(_Strict):
    """Aggregate stats for a completed backtest."""

    total_markets: int
    total_bets: int
    bets_won: int
    bets_lost: int
    bets_void: int = 0
    strike_rate: float
    total_stake: float
    total_liability: float
    total_pnl: float
    roi: float


class MarketResult(_Strict):
    """Per-market detail row included in the full result document."""

    market_id: str
    race_time: datetime
    venue: str | None = None
    country: str | None = None
    market_type: str | None = None
    selection_id: int
    runner: str
    bsp: float | None = None
    lay_price: float | None = None
    rule_applied: str
    side: Literal["LAY", "BACK"]
    stake: float
    liability: float
    outcome: Literal["WON", "LOST", "VOID"]
    pnl: float


class BacktestResult(_Strict):
    """Body of GET /api/results/{result_id} once a job has succeeded."""

    job_id: str
    status: JobStatus
    plugin: str
    plugin_version: str
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    source_mode: SourceType
    summary: BacktestSummary
    markets: list[MarketResult]


class JobRecord(_Strict):
    """The on-disk representation of a job's state.

    Held in memory while running and persisted to GCS on every transition
    so the API can report status even after a service restart.
    """

    job_id: str
    status: JobStatus
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    plugin: str
    plugin_version: str
    source_mode: SourceType
    progress: JobProgress = Field(default_factory=JobProgress)
    error: str | None = None
    request: BacktestRequest
    # Populated when the job reaches SUCCEEDED. Mirrored from the full
    # BacktestResult so the listing endpoint can show summary stats
    # without rehydrating result.json from GCS on every page load.
    summary: BacktestSummary | None = None


class ResultListItem(_Strict):
    """Compact summary returned by GET /api/results."""

    job_id: str
    plugin: str
    status: JobStatus
    submitted_at: datetime
    finished_at: datetime | None
    source_mode: SourceType
    # Date range the job ran over. Pulled from request.date_range (new
    # shape) or request.plugin.source.date_range (legacy inline source).
    # None when the request omits both — which only happens when the
    # service-level default is used and the request supplies none.
    date_range: DateRange | None = None
    total_markets: int | None = None
    total_bets: int | None = None
    bets_won: int | None = None
    bets_lost: int | None = None
    strike_rate: float | None = None
    total_pnl: float | None = None
    roi: float | None = None


class ResultListResponse(_Strict):
    """Body of GET /api/results."""

    items: list[ResultListItem]
    page: int
    page_size: int
    total: int


class PluginRuleSchema(_Strict):
    """Subset of a JSON schema used to render rule editor forms."""

    name: str
    type: str
    required: bool = False
    description: str | None = None
    default: Any = None
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None


class PluginSchema(_Strict):
    """Schema served by GET /api/plugins/{name}/schema.

    Returns one field group per top-level plugin section so the portal
    can render a dynamic config form. ``defaults`` carries the saved
    plugin's current values verbatim so the form can be pre-populated
    without a second request.
    """

    name: str
    version: str
    description: str | None = None
    source_fields: list[PluginRuleSchema]
    parser_fields: list[PluginRuleSchema]
    rule_fields: list[PluginRuleSchema]
    control_fields: list[PluginRuleSchema]
    staking_fields: list[PluginRuleSchema]
    defaults: dict[str, Any]


class PluginInfo(_Strict):
    """Listing entry returned by GET /api/plugins."""

    name: str
    version: str
    description: str | None = None
    rule_count: int


class AdminStatus(_Strict):
    """Body of GET /admin/status."""

    service: str
    version: str
    environment: str
    uptime_seconds: float
    timestamp: datetime


class AdminConfig(_Strict):
    """Body of GET /admin/config and PUT /admin/config."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    max_concurrent_jobs: int = Field(ge=1, le=8)
    activity_log_size: int = Field(ge=10, le=1000)
    default_source_bucket: str
    results_bucket: str


class AdminStats(_Strict):
    """Body of GET /admin/stats."""

    backtests_run: int
    markets_processed_total: int
    bets_recorded_total: int
    successful_jobs: int
    failed_jobs: int
    average_duration_seconds: float


class ActivityEvent(_Strict):
    """One entry in the recent-activity ring buffer."""

    timestamp: datetime
    event: str
    job_id: str | None = None
    detail: str | None = None


class ActivityResponse(_Strict):
    """Body of GET /admin/activity."""

    events: list[ActivityEvent]


class CredentialSecretStatus(_Strict):
    """One required secret's current status. Status only — never values."""

    secret_id: str
    configured: bool
    error: str | None = None


class CredentialStatusResponse(_Strict):
    """Body of ``GET /admin/credentials/status``.

    Reports whether the engine's bound credential bundle is fully provisioned
    in Secret Manager. Identifies the bundle by ``bundle_name`` (per-engine,
    per-sport) without surfacing any secret values.
    """

    bundle_name: str
    project: str
    configured: bool
    secrets: list[CredentialSecretStatus]
    retrieved_at: datetime


class ControlAction(str, Enum):
    """Recognised values for /admin/control/{action}."""

    CANCEL_JOB = "cancel_job"
    CLEAR_RESULTS_CACHE = "clear_results_cache"


class ControlResponse(_Strict):
    """Body of GET /admin/control/{action}."""

    action: ControlAction
    accepted: bool
    detail: str | None = None
