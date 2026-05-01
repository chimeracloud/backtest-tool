"""Betfair Historic Data API integration.

Provides on-demand download for date ranges that aren't yet mirrored in
GCS. The downloaded files can optionally be uploaded back to GCS for
reuse on future runs.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

import betfairlightweight

from core.logging import get_logger
from models.schemas import FileType
from services.gcs_service import GcsPath, GcsService
from services.secrets_service import BetfairCredentials, SecretsService

logger = get_logger(__name__)


class HistoricDataService:
    """Wraps :class:`betfairlightweight.APIClient.historic`.

    The class is responsible for: writing the cert/key bundle to disk,
    creating a logged-in :class:`betfairlightweight.APIClient`, listing the
    file paths that match the request, and downloading them to a local
    work directory. Optional GCS persistence is delegated to
    :class:`GcsService`.
    """

    def __init__(
        self,
        secrets: SecretsService | None = None,
        gcs: GcsService | None = None,
    ) -> None:
        self._secrets = secrets or SecretsService()
        self._gcs = gcs or GcsService()

    def list_files(
        self,
        date_range: tuple[date, date],
        sport: str,
        plan: str,
        countries: Iterable[str] = (),
        market_types: Iterable[str] = (),
        file_types: Iterable[FileType] = (FileType.MARKET,),
    ) -> list[str]:
        """Return remote file paths matching the supplied filters."""

        with self._authenticated_client() as trading:
            start, end = date_range
            response = trading.historic.get_file_list(
                sport=sport,
                plan=plan,
                from_day=start.day,
                from_month=start.month,
                from_year=start.year,
                to_day=end.day,
                to_month=end.month,
                to_year=end.year,
                market_types_collection=list(market_types) or None,
                countries_collection=list(countries) or None,
                file_type_collection=[ft.value for ft in file_types] or None,
            )
        files = list(response or [])
        logger.info(
            "betfair historic file list returned",
            extra={"count": len(files), "from": str(start), "to": str(end)},
        )
        return files

    def download_files(
        self,
        file_paths: Iterable[str],
        destination: Path,
        persist_to_bucket: str | None = None,
    ) -> list[Path]:
        """Download files to ``destination`` and return their local paths.

        Args:
            file_paths: File paths returned by :meth:`list_files`.
            destination: Local directory to write into; created if missing.
            persist_to_bucket: If provided, the downloaded file is also
                uploaded to ``gs://bucket[/prefix]/<filename>`` so future
                runs can use the GCS source mode.
        """

        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []

        with self._authenticated_client() as trading:
            for remote_path in file_paths:
                local_name = trading.historic.download_file(
                    file_path=remote_path,
                    store_directory=str(destination),
                )
                local_path = Path(local_name)
                downloaded.append(local_path)
                logger.info(
                    "betfair historic file downloaded",
                    extra={"remote": remote_path, "local": str(local_path)},
                )
                if persist_to_bucket:
                    target = GcsPath.parse(persist_to_bucket)
                    blob_name = (target.prefix + local_path.name).lstrip("/")
                    try:
                        self._gcs.upload_file(
                            target.bucket, blob_name, local_path
                        )
                    except RuntimeError:
                        logger.exception(
                            "failed to mirror historic file to GCS",
                            extra={
                                "bucket": target.bucket,
                                "blob_name": blob_name,
                            },
                        )
        return downloaded

    @contextmanager
    def _authenticated_client(self) -> Iterator[betfairlightweight.APIClient]:
        """Yield a logged-in betfairlightweight client.

        Cert/key material is written to a short-lived temp directory and
        cleaned up on exit so the bytes never live on disk longer than
        the API call needs them.
        """

        creds: BetfairCredentials = self._secrets.get_betfair_credentials()
        with tempfile.TemporaryDirectory(prefix="bf_certs_") as tmpdir:
            cert_dir = Path(tmpdir)
            cert_path = cert_dir / "client.crt"
            key_path = cert_dir / "client.key"
            cert_path.write_text(creds.cert_pem)
            key_path.write_text(creds.key_pem)
            cert_path.chmod(0o600)
            key_path.chmod(0o600)

            trading = betfairlightweight.APIClient(
                username=creds.username,
                password=creds.password,
                app_key=creds.app_key,
                certs=str(cert_dir),
            )
            try:
                trading.login()
                yield trading
            finally:
                try:
                    trading.logout()
                except Exception:
                    logger.warning("betfair logout failed", exc_info=True)
