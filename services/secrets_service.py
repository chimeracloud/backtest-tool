"""Thin wrapper over Google Secret Manager.

Used by the Betfair Historic source mode to obtain the certificate-based
login credentials we cannot embed in the image. Secret values are cached
in-process for the lifetime of the container — short-lived enough to
respect rotation while avoiding hammering the Secret Manager API.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from google.api_core.exceptions import NotFound, PermissionDenied
from google.cloud import secretmanager

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BetfairCredentials:
    """Credentials needed to authenticate against the Betfair APIs."""

    username: str
    password: str
    app_key: str
    cert_pem: str
    key_pem: str


class SecretsService:
    """Fetches and caches secrets from Google Secret Manager."""

    _BETFAIR_SECRETS = (
        "betfair-username",
        "betfair-password",
        "betfair-app-key",
        "betfair-cert-pem",
        "betfair-key-pem",
    )

    def __init__(self, project: str | None = None) -> None:
        self._project = project or get_settings().gcp_project
        self._client = secretmanager.SecretManagerServiceClient()
        self._cache: dict[str, str] = {}
        self._lock = Lock()

    def get_secret(self, secret_id: str, version: str = "latest") -> str:
        """Return the decoded value of a secret.

        Args:
            secret_id: Logical secret name (without the project prefix).
            version: Secret version, defaults to ``latest``.

        Raises:
            RuntimeError: if the secret is missing or access is denied.
        """

        with self._lock:
            cached = self._cache.get(secret_id)
            if cached is not None:
                return cached

        name = f"projects/{self._project}/secrets/{secret_id}/versions/{version}"
        try:
            response = self._client.access_secret_version(name=name)
        except NotFound as exc:
            raise RuntimeError(f"secret '{secret_id}' not found") from exc
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account lacks access to secret '{secret_id}'"
            ) from exc

        value = response.payload.data.decode("utf-8")
        with self._lock:
            self._cache[secret_id] = value
        return value

    def get_betfair_credentials(self) -> BetfairCredentials:
        """Load the full Betfair credential bundle from Secret Manager."""

        for required in self._BETFAIR_SECRETS:
            self.get_secret(required)
        return BetfairCredentials(
            username=self._cache["betfair-username"],
            password=self._cache["betfair-password"],
            app_key=self._cache["betfair-app-key"],
            cert_pem=self._cache["betfair-cert-pem"],
            key_pem=self._cache["betfair-key-pem"],
        )

    def credential_status(
        self, secret_ids: tuple[str, ...] | None = None
    ) -> dict[str, "object"]:
        """Return a status report for the engine's bound credential bundle.

        Status only — secret values are never read into the response. For
        each required secret we attempt access; the result is reported as
        ``configured: True | False`` along with the Secret Manager error
        kind (``not_found`` / ``permission_denied`` / etc) when access
        fails. The runtime service account requires
        ``secretmanager.versions.access`` for ``configured: True`` to be
        reported.
        """

        targets = secret_ids or self._BETFAIR_SECRETS
        secrets: list[dict[str, "object"]] = []
        any_missing = False
        for secret_id in targets:
            name = (
                f"projects/{self._project}/secrets/{secret_id}/versions/latest"
            )
            try:
                self._client.access_secret_version(name=name)
                secrets.append({"secret_id": secret_id, "configured": True})
            except NotFound:
                secrets.append(
                    {
                        "secret_id": secret_id,
                        "configured": False,
                        "error": "not_found",
                    }
                )
                any_missing = True
            except PermissionDenied:
                secrets.append(
                    {
                        "secret_id": secret_id,
                        "configured": False,
                        "error": "permission_denied",
                    }
                )
                any_missing = True
            except Exception as exc:  # noqa: BLE001 — surfaced to operator
                secrets.append(
                    {
                        "secret_id": secret_id,
                        "configured": False,
                        "error": type(exc).__name__,
                    }
                )
                any_missing = True
        return {
            "project": self._project,
            "configured": not any_missing,
            "secrets": secrets,
        }
