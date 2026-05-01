"""Structured JSON logging for Cloud Run.

Produces one-line JSON records with the fields Cloud Logging recognises so
log entries appear correctly in the GCP console without additional parsing.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger import json as jsonlogger

from core.config import get_settings

_CONFIGURED = False


class _CloudLoggingFormatter(jsonlogger.JsonFormatter):
    """JSON formatter that maps stdlib levels to Cloud Logging severities."""

    _LEVEL_TO_SEVERITY = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["severity"] = self._LEVEL_TO_SEVERITY.get(record.levelno, "DEFAULT")
        log_record["logger"] = record.name
        log_record.setdefault("service", get_settings().service_name)
        log_record.setdefault("version", get_settings().version)
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)


def configure_logging() -> None:
    """Install the JSON formatter on the root logger.

    Idempotent — repeated invocations are no-ops.
    """

    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _CloudLoggingFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for noisy in ("urllib3", "google", "betfairlightweight"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger after ensuring :func:`configure_logging` has run."""

    configure_logging()
    return logging.getLogger(name)
