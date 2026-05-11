"""Job state and result storage.

Job records are kept in memory for fast polling and persisted to GCS so
they survive process restarts. The GCS layout is::

    gs://chiops-backtest-results/jobs/{job_id}/status.json
    gs://chiops-backtest-results/jobs/{job_id}/result.json

This module deliberately exposes async methods even though the underlying
GCS client is synchronous — the work is pushed to a thread pool so the
event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.logging import get_logger
from models.schemas import (
    BacktestResult,
    JobRecord,
    JobStatus,
    ResultListItem,
    SourceType,
)
from services.gcs_service import GcsService

logger = get_logger(__name__)


class JobStore:
    """Thread-safe registry of in-flight and historic jobs."""

    _STATUS_PREFIX = "jobs/"
    _STATUS_FILENAME = "status.json"
    _RESULT_FILENAME = "result.json"
    # GCS hard-limits object mutation to roughly 1 write/sec per object.
    # Progress updates fire once per processed file; with many small files
    # the rate trips ``429 TooManyRequests`` on ``status.json``. Throttle
    # in-progress persists to at most one every 2s (terminal states
    # always persist immediately so the operator never misses a final
    # state transition).
    _PROGRESS_PERSIST_INTERVAL_S = 2.0
    _TERMINAL_STATUSES = frozenset(
        {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
    )

    def __init__(self, gcs: GcsService, results_bucket: str) -> None:
        self._gcs = gcs
        self._bucket = results_bucket
        self._jobs: dict[str, JobRecord] = {}
        self._results: dict[str, BacktestResult] = {}
        self._cancellations: set[str] = set()
        self._last_persist_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def register(self, record: JobRecord) -> None:
        """Add a freshly submitted job and persist its initial state."""

        async with self._lock:
            self._jobs[record.job_id] = record
        await self._persist_status(record)
        self._last_persist_at[record.job_id] = time.monotonic()

    async def update(self, record: JobRecord) -> None:
        """Replace the in-memory record; persist to GCS subject to throttle.

        In-memory state is always refreshed (so HTTP polls and SSE see
        live values). GCS writes are throttled to ``_PROGRESS_PERSIST_INTERVAL_S``
        to avoid the per-object ``429 TooManyRequests`` cap; terminal
        statuses bypass the throttle so the final state is always
        durable.
        """

        async with self._lock:
            self._jobs[record.job_id] = record

        is_terminal = record.status in self._TERMINAL_STATUSES
        if not is_terminal:
            last = self._last_persist_at.get(record.job_id, 0.0)
            if time.monotonic() - last < self._PROGRESS_PERSIST_INTERVAL_S:
                return
        await self._persist_status(record)
        self._last_persist_at[record.job_id] = time.monotonic()

    async def get(self, job_id: str) -> JobRecord | None:
        """Return the current record for ``job_id``, if known.

        Falls back to GCS for jobs that were created on a previous instance.
        """

        async with self._lock:
            cached = self._jobs.get(job_id)
        if cached is not None:
            return cached
        loaded = await self._load_status(job_id)
        if loaded is not None:
            async with self._lock:
                self._jobs.setdefault(job_id, loaded)
        return loaded

    async def save_result(self, job_id: str, result: BacktestResult) -> None:
        """Persist the full result document and cache it in memory."""

        async with self._lock:
            self._results[job_id] = result
        blob = f"{self._STATUS_PREFIX}{job_id}/{self._RESULT_FILENAME}"
        body = result.model_dump_json(by_alias=True).encode("utf-8")
        await asyncio.to_thread(
            self._gcs.upload_bytes,
            self._bucket,
            blob,
            body,
            content_type="application/json",
        )

    async def get_result(self, job_id: str) -> BacktestResult | None:
        """Return the full result document, loading from GCS on miss."""

        async with self._lock:
            cached = self._results.get(job_id)
        if cached is not None:
            return cached
        blob = f"{self._STATUS_PREFIX}{job_id}/{self._RESULT_FILENAME}"
        body = await asyncio.to_thread(self._gcs.download_bytes, self._bucket, blob)
        if body is None:
            return None
        try:
            data = json.loads(body)
            result = BacktestResult.model_validate(data)
        except (ValueError, TypeError) as exc:
            logger.exception("failed to parse stored result", extra={"job_id": job_id})
            raise RuntimeError(f"corrupt result document for job {job_id}") from exc
        async with self._lock:
            self._results[job_id] = result
        return result

    async def list_results(
        self,
        page: int,
        page_size: int,
        plugin: str | None = None,
        status: JobStatus | None = None,
    ) -> tuple[list[ResultListItem], int]:
        """Return a paginated list of recent jobs, newest first."""

        async with self._lock:
            jobs = list(self._jobs.values())
            results = dict(self._results)
        items: list[ResultListItem] = []
        for record in sorted(jobs, key=lambda r: r.submitted_at, reverse=True):
            if plugin is not None and record.plugin != plugin:
                continue
            if status is not None and record.status is not status:
                continue
            # Prefer the summary mirrored onto the record (survives restarts
            # when _results is empty); fall back to the in-memory result
            # document for any old job that hasn't been re-saved with the
            # mirrored field yet.
            res = results.get(record.job_id)
            summary = record.summary or (res.summary if res else None)
            # Date range comes from the request — new shape carries it at
            # the top level, legacy plugins embed it under plugin.source.
            req_range = record.request.date_range
            if req_range is None and record.request.plugin.source is not None:
                req_range = record.request.plugin.source.date_range
            items.append(
                ResultListItem(
                    job_id=record.job_id,
                    plugin=record.plugin,
                    status=record.status,
                    submitted_at=record.submitted_at,
                    finished_at=record.finished_at,
                    source_mode=record.source_mode,
                    date_range=req_range,
                    total_markets=summary.total_markets if summary else None,
                    total_bets=summary.total_bets if summary else None,
                    bets_won=summary.bets_won if summary else None,
                    bets_lost=summary.bets_lost if summary else None,
                    strike_rate=summary.strike_rate if summary else None,
                    total_pnl=summary.total_pnl if summary else None,
                    roi=summary.roi if summary else None,
                )
            )
        total = len(items)
        start = (page - 1) * page_size
        return items[start : start + page_size], total

    async def request_cancel(self, job_id: str) -> bool:
        """Mark a job for cancellation; the worker will check on next tick."""

        async with self._lock:
            if job_id not in self._jobs:
                return False
            self._cancellations.add(job_id)
        return True

    async def is_cancelled(self, job_id: str) -> bool:
        """True if a cancellation has been requested for ``job_id``."""

        async with self._lock:
            return job_id in self._cancellations

    async def clear_results_cache(self) -> None:
        """Drop in-memory caches; persisted GCS data is untouched."""

        async with self._lock:
            self._jobs.clear()
            self._results.clear()
            self._cancellations.clear()

    async def _persist_status(self, record: JobRecord) -> None:
        blob = f"{self._STATUS_PREFIX}{record.job_id}/{self._STATUS_FILENAME}"
        body = record.model_dump_json().encode("utf-8")
        await asyncio.to_thread(
            self._gcs.upload_bytes,
            self._bucket,
            blob,
            body,
            content_type="application/json",
        )

    async def _load_status(self, job_id: str) -> JobRecord | None:
        blob = f"{self._STATUS_PREFIX}{job_id}/{self._STATUS_FILENAME}"
        body = await asyncio.to_thread(self._gcs.download_bytes, self._bucket, blob)
        if body is None:
            return None
        try:
            return JobRecord.model_validate_json(body)
        except ValueError as exc:
            logger.exception("corrupt status document", extra={"job_id": job_id})
            raise RuntimeError(f"corrupt status document for job {job_id}") from exc


def make_job_id(now: datetime | None = None) -> str:
    """Return a sortable, opaque job identifier.

    Format: ``bt_YYYYMMDDHHMMSS_<random6>``. The timestamp prefix means the
    GCS object listing is naturally chronological.
    """

    import secrets

    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    return f"bt_{ts}_{secrets.token_hex(3)}"


_STORE: JobStore | None = None


def get_job_store() -> JobStore:
    """Return the lazily-initialised process-wide :class:`JobStore`."""

    global _STORE
    if _STORE is None:
        settings = get_settings()
        _STORE = JobStore(GcsService(), settings.results_bucket)
    return _STORE


def determine_source_mode(payload: dict[str, Any]) -> SourceType:
    """Return the discriminant value of a SourceConfig payload, defaulting to GCS."""

    return SourceType(payload.get("type", SourceType.GCS.value))
