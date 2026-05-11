"""FastAPI application exposing the three endpoint sets.

Endpoint sets:

* ``/admin/*`` — health, settings, metrics, control, SSE.
* ``/api/results/*`` and ``/api/plugins/*`` — GUI/portal-facing.
* ``/api/backtest/*`` — content endpoints called by AIM agents.

The module wires together the singletons defined in :mod:`core.config`,
:mod:`core.events`, :mod:`core.job_store`, and :mod:`core.plugin_store`,
and dispatches new jobs to a :class:`BacktestService` running in a worker
task.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Path as FastApiPath,
    Query,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from sse_starlette.sse import EventSourceResponse

from core.config import AppSettings, get_settings
from core.events import EventBus, get_event_bus
from core.job_store import (
    JobStore,
    determine_source_mode,
    get_job_store,
    make_job_id,
)
from core.logging import configure_logging, get_logger
from core.plugin_store import (
    PluginNotFoundError,
    PluginStore,
    get_plugin_store,
)
from models.schemas import (
    ActivityResponse,
    AdminConfig,
    AdminStats,
    AdminStatus,
    BacktestRequest,
    BacktestResult,
    BacktestSubmission,
    ControlAction,
    ControlResponse,
    CredentialSecretStatus,
    CredentialStatusResponse,
    JobRecord,
    JobStatus,
    PluginInfo,
    PluginSchema,
    ResultListResponse,
    SourceType,
)
from services.backtest_service import BacktestService
from services.gcs_service import GcsService
from services.historic_service import HistoricDataService

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise the background worker pool and shared singletons."""

    configure_logging()
    settings = get_settings()
    app.state.settings = settings
    app.state.started_at = time.monotonic()
    app.state.gcs = GcsService()
    app.state.historic = HistoricDataService()
    app.state.job_store = get_job_store()
    app.state.event_bus = get_event_bus()
    app.state.plugin_store = get_plugin_store()
    app.state.semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
    app.state.backtest_service = BacktestService(
        gcs=app.state.gcs,
        historic=app.state.historic,
        job_store=app.state.job_store,
        event_bus=app.state.event_bus,
    )
    logger.info(
        "service started",
        extra={
            "service": settings.service_name,
            "version": settings.version,
            "environment": settings.environment,
        },
    )
    try:
        yield
    finally:
        logger.info("service stopping")


app = FastAPI(
    title="Chimera Backtest Tool",
    version=get_settings().version,
    description=(
        "Stateless, AIM-compatible backtest tool. Submit a Betfair source + "
        "strategy plugin config and receive per-market results with P&L."
    ),
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)


def _settings_dep() -> AppSettings:
    return get_settings()


def _job_store_dep() -> JobStore:
    return get_job_store()


def _event_bus_dep() -> EventBus:
    return get_event_bus()


def _plugin_store_dep() -> PluginStore:
    return get_plugin_store()


# ---------------------------------------------------------------------------
# SET 1 — PARAMETERS (admin)
# ---------------------------------------------------------------------------


@app.get(
    "/admin/status",
    response_model=AdminStatus,
    tags=["admin"],
    summary="Service health, version, and uptime.",
)
async def admin_status(settings: AppSettings = Depends(_settings_dep)) -> AdminStatus:
    """Return basic liveness information for the running container."""

    return AdminStatus(
        service=settings.service_name,
        version=settings.version,
        environment=settings.environment,
        uptime_seconds=time.monotonic() - app.state.started_at,
        timestamp=datetime.now(timezone.utc),
    )


@app.get(
    "/admin/config",
    response_model=AdminConfig,
    tags=["admin"],
    summary="Effective default settings as structured JSON.",
)
async def admin_config_get(
    settings: AppSettings = Depends(_settings_dep),
) -> AdminConfig:
    """Return the running service configuration."""

    return AdminConfig(
        log_level=settings.log_level,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        activity_log_size=settings.activity_log_size,
        default_source_bucket=settings.default_source_bucket,
        results_bucket=settings.results_bucket,
    )


@app.put(
    "/admin/config",
    response_model=AdminConfig,
    tags=["admin"],
    summary="Update mutable defaults (in-memory only).",
)
async def admin_config_put(
    payload: AdminConfig,
    settings: AppSettings = Depends(_settings_dep),
) -> AdminConfig:
    """Apply runtime configuration changes.

    Mutable runtime fields are updated in-process. ``max_concurrent_jobs``
    is intentionally **not** hot-swapped: the semaphore guards in-flight
    jobs and replacing it under load can briefly violate the limit. The
    new value is recorded in :class:`AppSettings` and takes effect on the
    next container restart, so the deploy manifest remains the source of
    truth.
    """

    settings.log_level = payload.log_level
    settings.activity_log_size = payload.activity_log_size
    settings.default_source_bucket = payload.default_source_bucket
    settings.results_bucket = payload.results_bucket
    settings.max_concurrent_jobs = payload.max_concurrent_jobs
    return payload


@app.get(
    "/admin/stats",
    response_model=AdminStats,
    tags=["admin"],
    summary="Aggregate usage metrics since process start.",
)
async def admin_stats(bus: EventBus = Depends(_event_bus_dep)) -> AdminStats:
    """Return rolling counters for jobs, markets, and bets."""

    snapshot = await bus.stats_snapshot()
    return AdminStats(**snapshot)


@app.get(
    "/admin/credentials/status",
    response_model=CredentialStatusResponse,
    tags=["admin"],
    summary="Credential bundle status (no values surfaced).",
)
async def admin_credentials_status(
    settings: AppSettings = Depends(_settings_dep),
) -> CredentialStatusResponse:
    """Report whether the backtest tool's Secret Manager bundle is provisioned.

    Status only — secret values are never returned. Surfaces the same shape
    used by the live engine so the central Credentials Manager (under
    Administration on the portal) can read both with one client.
    """

    from services.secrets_service import SecretsService

    service = SecretsService()
    report = await asyncio.to_thread(service.credential_status)
    bundle_name = f"betfair-{settings.service_name}-creds"
    return CredentialStatusResponse(
        bundle_name=bundle_name,
        project=str(report.get("project", settings.gcp_project)),
        configured=bool(report.get("configured", False)),
        secrets=[
            CredentialSecretStatus(
                secret_id=str(s.get("secret_id", "")),
                configured=bool(s.get("configured", False)),
                error=(str(s["error"]) if s.get("error") else None),
            )
            for s in report.get("secrets", [])
        ],
        retrieved_at=datetime.now(timezone.utc),
    )


@app.get(
    "/admin/activity",
    response_model=ActivityResponse,
    tags=["admin"],
    summary="Recent activity log (newest first, capped by activity_log_size).",
)
async def admin_activity(bus: EventBus = Depends(_event_bus_dep)) -> ActivityResponse:
    """Return the in-memory activity ring buffer."""

    events = await bus.recent_activity()
    return ActivityResponse(events=events)


@app.get(
    "/admin/control/{action}",
    response_model=ControlResponse,
    tags=["admin"],
    summary="Service controls (cancel job, clear results cache).",
)
async def admin_control(
    action: ControlAction,
    job_id: str | None = Query(default=None, description="Required for cancel_job"),
    jobs: JobStore = Depends(_job_store_dep),
) -> ControlResponse:
    """Apply a one-shot control action and return whether it was accepted."""

    if action is ControlAction.CANCEL_JOB:
        if job_id is None:
            raise HTTPException(
                status_code=400,
                detail="cancel_job requires a job_id query parameter",
            )
        accepted = await jobs.request_cancel(job_id)
        return ControlResponse(
            action=action,
            accepted=accepted,
            detail=(
                f"cancellation requested for {job_id}"
                if accepted
                else f"unknown job {job_id}"
            ),
        )
    if action is ControlAction.CLEAR_RESULTS_CACHE:
        await jobs.clear_results_cache()
        return ControlResponse(
            action=action,
            accepted=True,
            detail="in-memory caches cleared",
        )
    raise HTTPException(status_code=400, detail=f"unknown action: {action}")


@app.get(
    "/admin/events",
    tags=["admin"],
    summary="Server-Sent Events stream of job lifecycle updates.",
)
async def admin_events(
    bus: EventBus = Depends(_event_bus_dep),
) -> EventSourceResponse:
    """Open an SSE connection that emits one event per state change."""

    async def _gen() -> AsyncIterator[dict[str, Any]]:
        async for record in bus.subscribe():
            yield {
                "event": record["event"],
                "data": json.dumps(
                    {
                        "timestamp": record["timestamp"],
                        "job_id": record.get("job_id"),
                        "data": record.get("data", {}),
                    }
                ),
            }

    return EventSourceResponse(_gen())


# ---------------------------------------------------------------------------
# SET 2 — GUI (portal-facing)
# ---------------------------------------------------------------------------


@app.get(
    "/api/results",
    response_model=ResultListResponse,
    tags=["gui"],
    summary="List completed backtest jobs with summary statistics.",
)
async def list_results(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    plugin: str | None = Query(default=None),
    status: JobStatus | None = Query(default=None),
    jobs: JobStore = Depends(_job_store_dep),
) -> ResultListResponse:
    """Return a paginated, filterable list of jobs."""

    items, total = await jobs.list_results(
        page=page, page_size=page_size, plugin=plugin, status=status
    )
    return ResultListResponse(
        items=items, page=page, page_size=page_size, total=total
    )


@app.get(
    "/api/results/{result_id}",
    response_model=BacktestResult,
    tags=["gui"],
    summary="Return the full result document for a job.",
)
async def get_result(
    result_id: str = FastApiPath(..., description="Job/result id."),
    jobs: JobStore = Depends(_job_store_dep),
) -> BacktestResult:
    """Return the per-market detail and summary for ``result_id``."""

    result = await jobs.get_result(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"result {result_id} not found")
    return result


class _MarketsBlock(BaseModel):
    job_id: str
    summary: dict[str, Any]
    markets: list[dict[str, Any]]


@app.get(
    "/api/results/{result_id}/markets",
    response_model=_MarketsBlock,
    tags=["gui"],
    summary="Per-market breakdown formatted for portal table rendering.",
)
async def get_result_markets(
    result_id: str,
    jobs: JobStore = Depends(_job_store_dep),
) -> _MarketsBlock:
    """Return only the per-market rows of a result document."""

    result = await jobs.get_result(result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"result {result_id} not found")
    return _MarketsBlock(
        job_id=result.job_id,
        summary=result.summary.model_dump(),
        markets=[m.model_dump() for m in result.markets],
    )


@app.get(
    "/api/plugins",
    response_model=list[PluginInfo],
    tags=["gui"],
    summary="List installed strategy plugins.",
)
async def list_plugins(
    plugins: PluginStore = Depends(_plugin_store_dep),
) -> list[PluginInfo]:
    """Return a summary entry per installed plugin."""

    return plugins.list()


@app.get(
    "/api/plugins/{plugin_name}/schema",
    response_model=PluginSchema,
    tags=["gui"],
    summary="Editor schema for a specific plugin.",
)
async def get_plugin_schema(
    plugin_name: str,
    plugins: PluginStore = Depends(_plugin_store_dep),
) -> PluginSchema:
    """Return the schema used by the portal to render a config form."""

    try:
        return plugins.schema_for(plugin_name)
    except PluginNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"plugin '{plugin_name}' not installed"
        ) from exc


# ---------------------------------------------------------------------------
# SET 3 — CONTENT (the AIM agent calls these)
# ---------------------------------------------------------------------------


@app.post(
    "/api/backtest",
    response_model=BacktestSubmission,
    status_code=202,
    tags=["content"],
    summary="Submit a backtest job; returns job_id immediately.",
)
async def submit_backtest(
    request: BacktestRequest,
    background_tasks: BackgroundTasks,
    plugins: PluginStore = Depends(_plugin_store_dep),
    jobs: JobStore = Depends(_job_store_dep),
    bus: EventBus = Depends(_event_bus_dep),
) -> BacktestSubmission:
    """Validate the request, register the job, and dispatch a worker task."""

    if request.plugin.name not in {p.name for p in plugins.list()}:
        logger.info(
            "plugin not in registry — accepting inline definition",
            extra={"plugin": request.plugin.name},
        )

    job_id = make_job_id()
    submitted_at = datetime.now(timezone.utc)
    # Source can live on plugin.source (legacy), request.source (override),
    # or fall through to the admin default GCS bucket. Mirror that order
    # when deciding the source mode tag persisted on the job record.
    if request.plugin.source is not None:
        source_payload = request.plugin.source.model_dump(mode="json")
    elif request.source is not None:
        source_payload = request.source.model_dump(mode="json")
    else:
        source_payload = {"type": SourceType.GCS.value}
    record = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        submitted_at=submitted_at,
        plugin=request.plugin.name,
        plugin_version=request.plugin.version,
        source_mode=determine_source_mode(source_payload),
        request=request,
    )
    await jobs.register(record)
    await bus.publish(
        "job_submitted",
        {
            "plugin": request.plugin.name,
            "source_mode": record.source_mode.value,
        },
        job_id=job_id,
        detail=f"job submitted, plugin={request.plugin.name}",
    )

    background_tasks.add_task(_run_with_semaphore, job_id, record)

    return BacktestSubmission(
        job_id=job_id,
        status=JobStatus.QUEUED,
        submitted_at=submitted_at,
        plugin=request.plugin.name,
    )


async def _run_with_semaphore(job_id: str, record: JobRecord) -> None:
    """Acquire the concurrency semaphore and execute the job."""

    semaphore: asyncio.Semaphore = app.state.semaphore
    service: BacktestService = app.state.backtest_service
    async with semaphore:
        try:
            await service.run(record)
        except Exception:
            logger.exception(
                "backtest worker raised unexpectedly",
                extra={"job_id": job_id},
            )


@app.get(
    "/api/backtest/{job_id}",
    response_model=JobRecord,
    tags=["content"],
    summary="Poll the status of a submitted job.",
)
async def get_backtest_status(
    job_id: str,
    jobs: JobStore = Depends(_job_store_dep),
) -> JobRecord:
    """Return the current :class:`JobRecord` for ``job_id``."""

    record = await jobs.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return record


@app.get(
    "/api/backtest/{job_id}/download/{format}",
    tags=["content"],
    summary="Download a completed result as JSON, xlsx, or parquet.",
)
async def download_backtest(
    job_id: str,
    format: str,
    jobs: JobStore = Depends(_job_store_dep),
) -> Response:
    """Render the result document in the requested format."""

    fmt = format.lower()
    if fmt not in {"json", "xlsx", "parquet"}:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format '{format}' (use json | xlsx | parquet)",
        )
    result = await jobs.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"result {job_id} not found")

    if fmt == "json":
        return JSONResponse(
            content=json.loads(result.model_dump_json()),
            headers={
                "Content-Disposition": f"attachment; filename={job_id}.json"
            },
        )
    if fmt == "xlsx":
        buffer = _render_xlsx(result)
        return StreamingResponse(
            buffer,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f"attachment; filename={job_id}.xlsx"
            },
        )
    buffer = _render_parquet(result)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.apache.parquet",
        headers={
            "Content-Disposition": f"attachment; filename={job_id}.parquet"
        },
    )


def _render_xlsx(result: BacktestResult) -> io.BytesIO:
    """Serialise a :class:`BacktestResult` to an xlsx workbook."""

    workbook = openpyxl.Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "Summary"
    summary_sheet.append(["Field", "Value"])
    summary_sheet.append(["job_id", result.job_id])
    summary_sheet.append(["plugin", f"{result.plugin}@{result.plugin_version}"])
    summary_sheet.append(["status", result.status.value])
    summary_sheet.append(
        [
            "duration_seconds",
            float(result.duration_seconds) if result.duration_seconds else 0.0,
        ]
    )
    for key, value in result.summary.model_dump().items():
        summary_sheet.append([key, value])

    markets_sheet = workbook.create_sheet("Markets")
    headers = list(result.markets[0].model_dump().keys()) if result.markets else []
    markets_sheet.append(headers)
    for row in result.markets:
        markets_sheet.append([_xlsx_safe(row.model_dump()[h]) for h in headers])

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def _xlsx_safe(value: Any) -> Any:
    """openpyxl can't serialise enums or datetimes with timezone awareness."""

    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if hasattr(value, "value"):
        return getattr(value, "value")
    return value


def _render_parquet(result: BacktestResult) -> io.BytesIO:
    """Serialise the per-market table of a result to parquet bytes."""

    rows = [m.model_dump() for m in result.markets]
    if not rows:
        rows = [{"market_id": "", "stake": 0.0, "pnl": 0.0}]
    table = pa.Table.from_pylist(rows)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    buffer.seek(0)
    return buffer


@app.exception_handler(ValidationError)
async def _validation_handler(_request, exc: ValidationError) -> JSONResponse:  # type: ignore[no-untyped-def]
    """Return a 422 with structured error detail rather than a 500."""

    return JSONResponse(status_code=422, content={"errors": exc.errors()})
