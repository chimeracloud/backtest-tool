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
        countries: Iterable[str] = (),
        market_types: Iterable[str] = (),
    ) -> list[str]:
        """Return blob names that match the supplied filters.

        The Betfair-supplied buckets store files in a ``YYYY/MM/DD/...``
        layout per market, with the last two segments being the country
        code and the file containing the market type. Filters are applied
        in-memory after listing because GCS prefix listing cannot express
        regex predicates.
        """

        path = GcsPath.parse(gcs_uri)
        start, end = date_range
        country_set = {c.upper() for c in countries}
        type_set = {t.upper() for t in market_types}

        blobs: list[str] = []
        for day_offset in range((end - start).days + 1):
            current = start.fromordinal(start.toordinal() + day_offset)
            day_prefix = (
                f"{path.prefix}{current.year}/"
                f"{current.month:02d}/"
                f"{current.day:02d}/"
            ).lstrip("/")
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
                if not blob.name.endswith(".bz2"):
                    continue
                if country_set and not _country_matches(blob.name, country_set):
                    continue
                if type_set and not _market_type_matches(blob.name, type_set):
                    continue
                blobs.append(blob.name)

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


def _country_matches(blob_name: str, countries: set[str]) -> bool:
    """True if any country code segment of ``blob_name`` is in ``countries``."""

    upper = blob_name.upper()
    return any(f"/{c}/" in upper or upper.endswith(f"/{c}") for c in countries)


def _market_type_matches(blob_name: str, market_types: set[str]) -> bool:
    """True if the blob name mentions one of the requested market types."""

    upper = blob_name.upper()
    return any(t in upper for t in market_types)
