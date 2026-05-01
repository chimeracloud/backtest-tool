"""Orchestrator for the end-to-end backtest workflow.

For one job:

1. Resolve and download source files (GCS bucket or Betfair Historic API).
2. Decompress each ``.bz2`` file into a streamable JSONL temp file.
3. Iterate market updates with :func:`betfairlightweight.streaming
   .create_historical_generator_stream` and pick the snapshot at
   ``time_before_off_seconds`` before market off-time for evaluation.
4. Hand each snapshot to :func:`evaluator.evaluate` and record decisions.
5. Wait for the market to close, then settle each bet using ``actual_sp``.
6. Aggregate per-market results into a :class:`BacktestResult` document
   and persist it via the :class:`JobStore`.

The orchestrator is async at the boundary but pushes the synchronous
betfairlightweight processing into a thread pool so the event loop is
not blocked.
"""

from __future__ import annotations

import asyncio
import bz2
import shutil
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import betfairlightweight
from betfairlightweight import StreamListener

from core.config import get_settings
from core.events import EventBus
from core.job_store import JobStore
from core.logging import get_logger
from evaluator import evaluate
from models.decisions import BetDecision, NoBet, Side
from models.schemas import (
    BacktestRequest,
    BacktestResult,
    BacktestSummary,
    BetfairHistoricSourceConfig,
    GcsSourceConfig,
    JobProgress,
    JobRecord,
    JobStatus,
    MarketResult,
    SourceType,
)
from services.gcs_service import GcsPath, GcsService
from services.historic_service import HistoricDataService

logger = get_logger(__name__)


class BacktestService:
    """Orchestrates a single backtest job from request to persisted result."""

    def __init__(
        self,
        gcs: GcsService,
        historic: HistoricDataService,
        job_store: JobStore,
        event_bus: EventBus,
    ) -> None:
        self._gcs = gcs
        self._historic = historic
        self._jobs = job_store
        self._events = event_bus

    async def run(self, record: JobRecord) -> None:
        """Execute the job described by ``record``.

        Updates the supplied :class:`JobRecord` in place and persists state
        transitions through the :class:`JobStore`. Any uncaught exception
        is captured and the job is marked ``FAILED``; this method never
        re-raises.
        """

        await self._events.publish(
            "job_started",
            {"plugin": record.plugin, "source_mode": record.source_mode.value},
            job_id=record.job_id,
            detail=f"job started, plugin={record.plugin}",
        )
        record.status = JobStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        await self._jobs.update(record)

        work_root = get_settings().work_dir / record.job_id
        work_root.mkdir(parents=True, exist_ok=True)

        markets_processed = 0
        bets_recorded = 0
        try:
            local_files = await self._materialise_sources(record, work_root)
            if not local_files:
                raise RuntimeError(
                    "no files matched the supplied source configuration"
                )

            record.progress.files_total = len(local_files)
            await self._jobs.update(record)

            market_rows: list[MarketResult] = []
            for file_idx, local_file in enumerate(local_files, start=1):
                if await self._jobs.is_cancelled(record.job_id):
                    record.status = JobStatus.CANCELLED
                    await self._events.publish(
                        "job_cancelled",
                        {},
                        job_id=record.job_id,
                        detail="job cancelled by operator",
                    )
                    break

                file_rows = await asyncio.to_thread(
                    self._process_file, local_file, record.request
                )
                market_rows.extend(file_rows)
                markets_in_file = len({row.market_id for row in file_rows})
                markets_processed += markets_in_file
                bets_recorded += sum(1 for r in file_rows if r.outcome != "VOID")

                record.progress = JobProgress(
                    files_total=len(local_files),
                    files_processed=file_idx,
                    markets_total=markets_processed,
                    markets_processed=markets_processed,
                    bets_recorded=bets_recorded,
                    percentage=round(file_idx * 100.0 / len(local_files), 2),
                )
                await self._jobs.update(record)
                await self._events.publish(
                    "job_progress",
                    record.progress.model_dump(),
                    job_id=record.job_id,
                    detail=f"processed {file_idx}/{len(local_files)} files",
                )

            if record.status is not JobStatus.CANCELLED:
                summary = _summarise(market_rows)
                finished_at = datetime.now(timezone.utc)
                duration = (finished_at - record.started_at).total_seconds()
                result = BacktestResult(
                    job_id=record.job_id,
                    status=JobStatus.SUCCEEDED,
                    plugin=record.plugin,
                    plugin_version=record.plugin_version,
                    submitted_at=record.submitted_at,
                    started_at=record.started_at,
                    finished_at=finished_at,
                    duration_seconds=duration,
                    source_mode=record.source_mode,
                    summary=summary,
                    markets=market_rows,
                )
                await self._jobs.save_result(record.job_id, result)
                record.status = JobStatus.SUCCEEDED
                record.finished_at = finished_at
                await self._jobs.update(record)
                await self._events.publish(
                    "job_completed",
                    {
                        "summary": summary.model_dump(),
                        "duration_seconds": duration,
                    },
                    job_id=record.job_id,
                    detail=(
                        f"job succeeded: {summary.total_bets} bets, "
                        f"pnl={summary.total_pnl}"
                    ),
                )
                await self._events.record_job_finished(
                    succeeded=True,
                    markets_processed=summary.total_markets,
                    bets_recorded=summary.total_bets,
                    duration_seconds=duration,
                )

        except Exception as exc:
            record.status = JobStatus.FAILED
            record.finished_at = datetime.now(timezone.utc)
            record.error = f"{type(exc).__name__}: {exc}"
            stack_trace = traceback.format_exc()
            await self._jobs.update(record)
            logger.exception(
                "backtest job failed",
                extra={"job_id": record.job_id, "plugin": record.plugin},
            )
            duration = 0.0
            if record.started_at:
                duration = (record.finished_at - record.started_at).total_seconds()
            await self._events.publish(
                "job_failed",
                {
                    "error": record.error,
                    "stack_trace": stack_trace,
                    "duration_seconds": duration,
                },
                job_id=record.job_id,
                detail=record.error,
            )
            await self._events.record_job_finished(
                succeeded=False,
                markets_processed=markets_processed,
                bets_recorded=bets_recorded,
                duration_seconds=duration,
            )
        finally:
            shutil.rmtree(work_root, ignore_errors=True)

    async def _materialise_sources(
        self, record: JobRecord, work_dir: Path
    ) -> list[Path]:
        """Return local paths to every source file that should be processed.

        The source is read from the plugin block — the plugin is the
        complete instruction set and decides where data comes from.
        """

        source = record.request.plugin.source
        if isinstance(source, GcsSourceConfig):
            return await self._download_from_gcs(source, work_dir)
        if isinstance(source, BetfairHistoricSourceConfig):
            return await self._download_from_betfair(source, work_dir)
        raise TypeError(f"unsupported source type: {type(source).__name__}")

    async def _download_from_gcs(
        self, source: GcsSourceConfig, work_dir: Path
    ) -> list[Path]:
        path = GcsPath.parse(source.bucket)
        blobs = await asyncio.to_thread(
            self._gcs.list_historic_files,
            source.bucket,
            (source.date_range.start, source.date_range.end),
            source.filters.countries,
            source.filters.market_types,
        )
        local_files: list[Path] = []
        for blob_name in blobs:
            local = work_dir / Path(blob_name).name
            await asyncio.to_thread(self._gcs.download_blob, path.bucket, blob_name, local)
            local_files.append(local)
        return local_files

    async def _download_from_betfair(
        self, source: BetfairHistoricSourceConfig, work_dir: Path
    ) -> list[Path]:
        remote_files = await asyncio.to_thread(
            self._historic.list_files,
            (source.date_range.start, source.date_range.end),
            source.filters.sport,
            source.filters.plan,
            source.filters.countries,
            source.filters.market_types,
            source.filters.file_types,
        )
        local_files = await asyncio.to_thread(
            self._historic.download_files,
            remote_files,
            work_dir,
            source.persist_to_bucket,
        )
        return local_files

    def _process_file(
        self, local_file: Path, request: BacktestRequest
    ) -> list[MarketResult]:
        """Stream a single ``.bz2`` file and return per-market results.

        Runs synchronously in a worker thread.
        """

        if local_file.suffix == ".bz2":
            decompressed = local_file.with_suffix("")
            if not decompressed.exists():
                _decompress_bz2(local_file, decompressed)
            stream_path = decompressed
        else:
            stream_path = local_file

        rows: list[MarketResult] = []
        try:
            rows = self._stream_and_evaluate(stream_path, request)
        finally:
            for to_remove in {stream_path, local_file}:
                try:
                    to_remove.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "could not remove temp file",
                        extra={"path": str(to_remove)},
                    )
        return rows

    def _stream_and_evaluate(
        self, stream_path: Path, request: BacktestRequest
    ) -> list[MarketResult]:
        """Run the betfairlightweight generator over a single market file."""

        listener = StreamListener(max_latency=None)
        trading = betfairlightweight.APIClient("backtest", "backtest")
        stream = trading.streaming.create_historical_generator_stream(
            file_path=str(stream_path),
            listener=listener,
        )
        gen = stream.get_generator()

        plugin = request.plugin
        threshold = timedelta(seconds=plugin.parser.time_before_off_seconds)

        decisions_by_market: dict[str, list[BetDecision]] = {}
        evaluation_done: set[str] = set()
        last_books: dict[str, Any] = {}

        for market_books in gen():
            for market_book in market_books:
                market_id = market_book.market_id
                last_books[market_id] = market_book

                if market_id in evaluation_done:
                    continue

                md = getattr(market_book, "market_definition", None)
                if md is None or md.market_time is None:
                    continue
                publish_time = market_book.publish_time
                if publish_time is None:
                    continue

                seconds_to_off = (md.market_time - publish_time).total_seconds()
                if seconds_to_off > plugin.parser.time_before_off_seconds:
                    continue
                if seconds_to_off <= 0:
                    evaluation_done.add(market_id)
                    continue

                results = evaluate(
                    market_book,
                    plugin.strategy,
                    point_value=plugin.staking.point_value,
                    filters_country=plugin.source.filters.countries,
                    filters_market_type=plugin.source.filters.market_types,
                )
                bets = [r for r in results if isinstance(r, BetDecision)]
                if bets:
                    decisions_by_market[market_id] = bets
                else:
                    skip_reasons = [
                        r.reason for r in results if isinstance(r, NoBet)
                    ]
                    logger.debug(
                        "no bet for market",
                        extra={
                            "market_id": market_id,
                            "reason": skip_reasons[0] if skip_reasons else "unknown",
                        },
                    )
                evaluation_done.add(market_id)

        rows: list[MarketResult] = []
        for market_id, bets in decisions_by_market.items():
            settled_book = last_books.get(market_id)
            if settled_book is None:
                continue
            for bet in bets:
                row = _settle(bet, settled_book, plugin.parser.extract_bsp)
                if row is not None:
                    rows.append(row)
        return rows


def _decompress_bz2(src: Path, dst: Path) -> None:
    """Decompress ``src`` (``.bz2``) into ``dst``."""

    with bz2.open(src, "rb") as compressed, open(dst, "wb") as out:
        shutil.copyfileobj(compressed, out)


def _settle(
    bet: BetDecision, market_book: Any, extract_bsp: bool
) -> MarketResult | None:
    """Compute a :class:`MarketResult` for a single bet on a closed market."""

    md = getattr(market_book, "market_definition", None)
    if md is None:
        return None

    runner = next(
        (r for r in market_book.runners if r.selection_id == bet.selection_id),
        None,
    )
    runner_def = next(
        (r for r in (md.runners or []) if r.selection_id == bet.selection_id),
        None,
    )

    if runner is None and runner_def is None:
        return None

    runner_status = (
        getattr(runner, "status", None)
        or getattr(runner_def, "status", None)
        or "ACTIVE"
    )
    bsp = None
    if extract_bsp and runner is not None and getattr(runner, "sp", None):
        bsp = runner.sp.actual_sp
    if bsp is None and runner_def is not None:
        bsp = getattr(runner_def, "bsp", None)

    settlement_price = bsp if bsp else bet.price

    if runner_status == "REMOVED":
        outcome = "VOID"
        pnl = 0.0
    elif bet.side is Side.LAY:
        if runner_status == "WINNER":
            outcome = "LOST"
            pnl = -round(bet.stake * (settlement_price - 1.0), 2)
        elif runner_status == "LOSER":
            outcome = "WON"
            pnl = round(bet.stake, 2)
        else:
            outcome = "VOID"
            pnl = 0.0
    else:
        if runner_status == "WINNER":
            outcome = "WON"
            pnl = round(bet.stake * (settlement_price - 1.0), 2)
        elif runner_status == "LOSER":
            outcome = "LOST"
            pnl = -round(bet.stake, 2)
        else:
            outcome = "VOID"
            pnl = 0.0

    return MarketResult(
        market_id=market_book.market_id,
        race_time=md.market_time,
        venue=md.venue,
        country=md.country_code,
        market_type=md.market_type,
        selection_id=bet.selection_id,
        runner=bet.runner_name,
        bsp=float(bsp) if bsp is not None else None,
        lay_price=float(bet.price) if bet.side is Side.LAY else None,
        rule_applied=bet.rule_applied,
        side=bet.side.value,
        stake=bet.stake,
        liability=bet.liability,
        outcome=outcome,
        pnl=pnl,
    )


def _summarise(rows: list[MarketResult]) -> BacktestSummary:
    """Aggregate per-market rows into a :class:`BacktestSummary`."""

    total_markets = len({row.market_id for row in rows})
    total_bets = len(rows)
    bets_won = sum(1 for r in rows if r.outcome == "WON")
    bets_lost = sum(1 for r in rows if r.outcome == "LOST")
    bets_void = sum(1 for r in rows if r.outcome == "VOID")
    total_stake = round(sum(r.stake for r in rows), 2)
    total_liability = round(sum(r.liability for r in rows), 2)
    total_pnl = round(sum(r.pnl for r in rows), 2)
    decisive = bets_won + bets_lost
    strike_rate = round(bets_won / decisive, 4) if decisive else 0.0
    roi = round(total_pnl / total_stake, 4) if total_stake else 0.0
    return BacktestSummary(
        total_markets=total_markets,
        total_bets=total_bets,
        bets_won=bets_won,
        bets_lost=bets_lost,
        bets_void=bets_void,
        strike_rate=strike_rate,
        total_stake=total_stake,
        total_liability=total_liability,
        total_pnl=total_pnl,
        roi=roi,
    )
