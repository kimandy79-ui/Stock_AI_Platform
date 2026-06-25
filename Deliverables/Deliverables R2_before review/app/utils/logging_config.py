"""Logging configuration for the Swing Trading Stock Analyzer.

Implements the logging contract from ``CODING_STANDARDS.md`` section 5::

    timestamp | level | module | run_id | message

The ``run_id`` is a per-run UUID4 string (CODING_STANDARDS.md section 4).
Because the standard library ``LogRecord`` has no ``run_id`` attribute, this
module installs a logging filter that injects a default ``run_id`` when one is
not supplied, and provides :class:`RunIdLoggerAdapter` so callers can bind a
``run_id`` to every message they emit.

Scope rules (Module 01): no DB, no provider calls. This module only configures
the Python logging system and writes to the data/logs directory.

Per CODING_STANDARDS.md, library/service modules must not ``print``; they log.
"""

from __future__ import annotations

import logging
from logging import Handler, Logger, LoggerAdapter
from pathlib import Path
from typing import Any, Final

from app.config import constants, settings

# Default log file inside the data/logs directory.
DEFAULT_LOG_FILENAME: Final[str] = "stock_ai_platform.log"

# Module-level guard so configuration is applied at most once per process.
_CONFIGURED: bool = False


class _RunIdFilter(logging.Filter):
    """Ensure every record has a ``run_id`` attribute.

    Records emitted through :class:`RunIdLoggerAdapter` already carry a
    ``run_id``; direct ``logger.info(...)`` calls do not. This filter supplies
    a default so the formatter never raises ``KeyError``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = constants.DEFAULT_RUN_ID
        return True


class RunIdLoggerAdapter(LoggerAdapter):
    """Logger adapter that binds a ``run_id`` to every emitted record.

    Usage::

        base = logging.getLogger("app.services.features")
        log = RunIdLoggerAdapter(base, run_id)
        log.info("start")  # -> ... | app.services.features | <run_id> | start
    """

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        extra = dict(kwargs.get("extra") or {})
        extra.setdefault("run_id", self.extra.get("run_id", constants.DEFAULT_RUN_ID))
        kwargs["extra"] = extra
        return msg, kwargs


def _build_formatter() -> logging.Formatter:
    """Return a formatter using the project log format and ISO timestamps."""
    return logging.Formatter(fmt=constants.LOG_FORMAT, datefmt=constants.LOG_DATE_FORMAT)


def configure_logging(
    *,
    level: int = logging.INFO,
    log_to_file: bool = True,
    log_file: Path | None = None,
    force: bool = False,
) -> Logger:
    """Configure the root logger for the application.

    Parameters
    ----------
    level:
        Root logging level. Defaults to ``logging.INFO``.
    log_to_file:
        If ``True``, attach a file handler under ``data/logs``.
    log_file:
        Explicit log file path. Defaults to ``data/logs/stock_ai_platform.log``.
    force:
        If ``True``, reconfigure even if already configured (replaces handlers).

    Returns
    -------
    logging.Logger
        The configured root logger.
    """
    global _CONFIGURED

    root = logging.getLogger()
    if _CONFIGURED and not force:
        return root

    # Clear existing handlers to guarantee a deterministic configuration.
    for existing in list(root.handlers):
        root.removeHandler(existing)

    root.setLevel(level)
    formatter = _build_formatter()
    run_id_filter = _RunIdFilter()

    handlers: list[Handler] = []

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(run_id_filter)
    handlers.append(console)

    if log_to_file:
        settings.ensure_directories()
        target = log_file if log_file is not None else (settings.LOGS_DIR / DEFAULT_LOG_FILENAME)
        file_handler = logging.FileHandler(target, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(run_id_filter)
        handlers.append(file_handler)

    for handler in handlers:
        root.addHandler(handler)

    _CONFIGURED = True
    return root


def get_logger(name: str, run_id: str | None = None) -> RunIdLoggerAdapter:
    """Return a ``run_id``-aware logger adapter for ``name``.

    Parameters
    ----------
    name:
        Logger name, conventionally the module dotted path.
    run_id:
        UUID4 run identifier to bind. Defaults to the placeholder ``-`` when
        no run is active (e.g. during configuration or tests).
    """
    base = logging.getLogger(name)
    bound = run_id if run_id is not None else constants.DEFAULT_RUN_ID
    return RunIdLoggerAdapter(base, {"run_id": bound})


def is_configured() -> bool:
    """Return ``True`` if :func:`configure_logging` has run in this process."""
    return _CONFIGURED
