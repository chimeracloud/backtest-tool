"""Runtime configuration for the Chimera Backtest Tool.

Settings are loaded from environment variables and validated via Pydantic.
A single AppSettings instance is exposed via :func:`get_settings` and is
intended to be cached for the lifetime of the process.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Top-level application settings.

    All values are optional with sensible defaults so the service can boot
    in any environment that has the required GCP IAM bindings in place.
    """

    model_config = SettingsConfigDict(
        env_prefix="CHIMERA_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = Field(
        default="chimera-backtest-tool",
        description="Logical name of the service, used for logs and traces.",
    )
    version: str = Field(
        default="1.0.0",
        description="Semantic version of the deployed service.",
    )
    environment: Literal["development", "staging", "production"] = Field(
        default="production",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")

    gcp_project: str = Field(
        default="chiops",
        description="GCP project that hosts secrets and result storage.",
    )
    gcp_region: str = Field(default="europe-west2")

    results_bucket: str = Field(
        default="chiops-backtest-results",
        description="GCS bucket where job state and results are persisted.",
    )
    default_source_bucket: str = Field(
        default="gs://betfair-basic-historic/",
        description="Default GCS source bucket for historic .bz2 files.",
    )

    plugins_dir: Path = Field(
        default=Path(__file__).resolve().parent.parent / "plugins",
        description="Directory scanned for strategy plugin JSON files.",
    )
    work_dir: Path = Field(
        default=Path("/tmp/chimera-backtest"),
        description="Scratch directory used for temporary file downloads.",
    )

    max_concurrent_jobs: int = Field(
        default=2,
        ge=1,
        le=8,
        description="Cap on concurrent backtest jobs running in-process.",
    )
    activity_log_size: int = Field(
        default=200,
        ge=10,
        le=1000,
        description="Maximum number of activity events held in memory.",
    )
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "https://chimerasportstrading.com",
            "https://www.chimerasportstrading.com",
        ],
        description=(
            "Origins permitted to issue cross-origin requests against the API. "
            "Set CHIMERA_CORS_ALLOWED_ORIGINS to a JSON array to override."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return the process-wide :class:`AppSettings` singleton."""

    settings = AppSettings()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return settings
