"""Structured logging setup.

JSON by default so the harness output is greppable next to the Go service's
slog output, with a human renderer available for local runs.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure(level: str = "info", fmt: str = "json") -> None:
    numeric = _LEVELS.get(level.lower(), logging.INFO)
    renderer: Any = (
        structlog.dev.ConsoleRenderer() if fmt == "console" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )


def get(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
