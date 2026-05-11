"""Helpers for reading source data from and writing results to GCS.

Encapsulates all knowledge of the Cloud Storage layout, prefix parsing,
and download/upload mechanics so the rest of the codebase can stay
focussed on backtesting logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from google.api_core.exceptions import NotFound, PermissionDenied
from google.cloud import storage
from google.cloud.storage.blob import Blob

from core.logging import get_logger

logger = get_logger(__name__)

_BUCKET_RE = re.compile(r"^gs://(?P<bucket>[^/]+)/(?P<prefix>.*)$")


@dataclass(frozen=True, slots=True)
class GcsPath:
    """A parsed ``gs://bucket/prefix`` URI."""

    bucket: str
    prefix: str

    @classmethod
    def parse(cls, uri: str) -> "GcsPath":
        """Parse ``gs://bucket/prefix`` (the prefix may be empty)."""

        if not uri.startswith("gs://"):
            uri = f"gs://{uri.lstrip('/')}"
        match = _BUCKET_RE.match(uri.rstrip("/") + "/")
        if not match:
            raise ValueError(f"invalid GCS URI: {uri!r}")
        return cls(bucket=match.group("bucket"), prefix=match.group("prefix"))


class GcsService:
    """Synchronous GCS access — wrap calls in ``asyncio.to_thread`` from async code."""

    def __init__(self, client: storage.Client | None = None) -> None:
        self._client = client or storage.Client()

    def list_historic_files(
        self,
        gcs_uri: str,
        date_range: tuple[date, date],
        countries: Iterable[str] = (),  # noqa: ARG002 — accepted for API compat, filtered at parse time
        market_types: Iterable[str] = (),  # noqa: ARG002 — accepted for API compat, filtered at parse time
    ) -> list[str]:
        """Return ``.bz2`` blob names whose day-prefix falls inside ``date_range``.

        The Betfair-supplied buckets store files in a
        ``{TIER}/{YYYY}/{MonAbbr}/{D}/{EVENT_ID}/{MARKET_ID}.bz2`` layout
        — ``MonAbbr`` is a three-letter English month abbreviation
        (``Jan`` … ``Dec``) and ``D`` is the unpadded day-of-month.

        Country/market-type filters are NOT applied here. The day prefix
        carries no country segment, and the file name carries no market
        type — both must be read from each file's market metadata at
        parse time (see ``evaluator.evaluate`` which already filters on
        ``filters_country`` / ``filters_market_type`` once the market
        definition is available).
        """

        path = GcsPath.parse(gcs_uri)
        start, end = date_range

        blobs: list[str] = []
        prefixes_tried: list[str] = []
        for day_offset in range((end - start).days + 1):
            current = start.fromordinal(start.toordinal() + day_offset)
            # ``%b`` is the locale-aware month abbreviation. Cloud Run images
            # run in the C/POSIX locale so this yields English 3-letter
            # abbreviations (``Jan`` … ``Dec``) — which is what the bucket
            # uses. ``current.day`` is unpadded by default (``1`` not ``01``).
            day_prefix = (
                f"{path.prefix}{current.year}/"
                f"{current.strftime('%b')}/"
                f"{current.day}/"
            ).lstrip("/")
            prefixes_tried.append(day_prefix)
            try:
                day_blobs = self._client.list_blobs(path.bucket, prefix=day_prefix)
            except (NotFound, PermissionDenied) as exc:
                logger.error(
                    "failed to list GCS prefix",
                    extra={"bucket": path.bucket, "prefix": day_prefix, "error": str(exc)},
                )
                raise RuntimeError(
                    f"cannot list gs://{path.bucket}/{day_prefix}: {exc}"
                ) from exc
            for blob in day_blobs:
                if blob.name.endswith(".bz2"):
                    blobs.append(blob.name)

        if not blobs:
            sample = prefixes_tried[:3] + (["…"] if len(prefixes_tried) > 3 else [])
            logger.warning(
                "no historic files matched date range",
                extra={
                    "bucket": path.bucket,
                    "date_range": [start.isoformat(), end.isoformat()],
                    "prefixes_tried_sample": sample,
                    "prefix_count": len(prefixes_tried),
                },
            )
        else:
            logger.info(
                "listed historic files",
                extra={"bucket": path.bucket, "count": len(blobs)},
            )
        return blobs

    def download_blob(self, bucket: str, blob_name: str, destination: Path) -> Path:
        """Download a blob to disk and return the resulting path."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob: Blob = self._client.bucket(bucket).blob(blob_name)
            blob.download_to_filename(str(destination))
        except NotFound as exc:
            raise RuntimeError(
                f"blob gs://{bucket}/{blob_name} not found"
            ) from exc
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account cannot read gs://{bucket}/{blob_name}"
            ) from exc
        return destination

    def upload_bytes(
        self,
        bucket: str,
        blob_name: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload an in-memory byte buffer to a GCS blob."""

        try:
            blob = self._client.bucket(bucket).blob(blob_name)
            blob.upload_from_string(data, content_type=content_type)
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account cannot write gs://{bucket}/{blob_name}"
            ) from exc

    def download_bytes(self, bucket: str, blob_name: str) -> bytes | None:
        """Return blob contents as bytes, or ``None`` if the blob is missing."""

        try:
            blob = self._client.bucket(bucket).blob(blob_name)
            return blob.download_as_bytes()
        except NotFound:
            return None
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account cannot read gs://{bucket}/{blob_name}"
            ) from exc

    def upload_file(
        self,
        bucket: str,
        blob_name: str,
        source: Path,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload a local file to GCS — used to persist on-demand downloads."""

        try:
            blob = self._client.bucket(bucket).blob(blob_name)
            blob.upload_from_filename(str(source), content_type=content_type)
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account cannot write gs://{bucket}/{blob_name}"
            ) from exc


