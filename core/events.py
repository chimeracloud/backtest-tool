"""In-process event bus used to fan out SSE updates and feed activity logs.

The bus is intentionally simple — there is no persistence and no cross-
instance distribution. Cloud Run is configured to run a single instance
of this service which is sufficient for the tool's expected concurrency.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.logging import get_logger
from models.schemas import ActivityEvent

logger = get_logger(__name__)


class EventBus:
    """Pub/sub primitive backed by per-subscriber asyncio queues."""

    def __init__(self, activity_log_size: int) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._activity: deque[ActivityEvent] = deque(maxlen=activity_log_size)
        self._lock = asyncio.Lock()
        self._stats = {
            "backtests_run": 0,
            "markets_processed_total": 0,
            "bets_recorded_total": 0,
            "successful_jobs": 0,
            "failed_jobs": 0,
            "duration_total_seconds": 0.0,
            "duration_samples": 0,
        }

    async def publish(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        job_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Publish an event to all current subscribers and the activity log.

        Args:
            event: Short event identifier (e.g. ``job_started``).
            payload: Optional structured data emitted to SSE subscribers.
            job_id: Optional job identifier the event relates to.
            detail: Optional human-readable description for the activity log.
        """

        record = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "data": payload or {},
        }

        async with self._lock:
            self._activity.appendleft(
                ActivityEvent(
                    timestamp=datetime.now(timezone.utc),
                    event=event,
                    job_id=job_id,
                    detail=detail,
                )
            )
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(record)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Async iterator yielding events for the lifetime of the connection."""

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                yield item
        finally:
            async with self._lock:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)

    async def recent_activity(self) -> list[ActivityEvent]:
        """Return a snapshot of the activity ring buffer (newest first)."""

        async with self._lock:
            return list(self._activity)

    async def record_job_finished(
        self,
        *,
        succeeded: bool,
        markets_processed: int,
        bets_recorded: int,
        duration_seconds: float,
    ) -> None:
        """Update aggregate stats after a job finishes."""

        async with self._lock:
            self._stats["backtests_run"] += 1
            self._stats["markets_processed_total"] += markets_processed
            self._stats["bets_recorded_total"] += bets_recorded
            if succeeded:
                self._stats["successful_jobs"] += 1
            else:
                self._stats["failed_jobs"] += 1
            self._stats["duration_total_seconds"] += duration_seconds
            self._stats["duration_samples"] += 1

    async def stats_snapshot(self) -> dict[str, Any]:
        """Return a copy of the current stats counters."""

        async with self._lock:
            samples = self._stats["duration_samples"]
            avg = (
                self._stats["duration_total_seconds"] / samples if samples else 0.0
            )
            return {
                "backtests_run": self._stats["backtests_run"],
                "markets_processed_total": self._stats["markets_processed_total"],
                "bets_recorded_total": self._stats["bets_recorded_total"],
                "successful_jobs": self._stats["successful_jobs"],
                "failed_jobs": self._stats["failed_jobs"],
                "average_duration_seconds": avg,
            }


_BUS: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the lazily-initialised process-wide :class:`EventBus`."""

    global _BUS
    if _BUS is None:
        _BUS = EventBus(activity_log_size=get_settings().activity_log_size)
    return _BUS
