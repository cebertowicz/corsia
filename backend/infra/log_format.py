"""Traceable formatting for backend and Uvicorn log records."""

from __future__ import annotations

import logging
import os
import sys
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG

LOGURU_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | pid {process.id} | {module}:{function}:{line} - {message}"
_LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)s | pid %(process)d | %(module)s:%(funcName)s:%(lineno)d - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def uvicorn_log_config() -> dict:
    """Preserve Uvicorn's handlers and levels while making its lines traceable."""
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"].update(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    config["formatters"]["access"].update(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)s | pid %(process)d | uvicorn.access - %(client_addr)s - \"%(request_line)s\" %(status_code)s",
        datefmt=_DATE_FORMAT,
    )
    return config


class _TraceFormatter(logging.Formatter):
    """Add trace context without replacing the operator's formatter output."""

    def __init__(self, original: logging.Formatter | None, *, access: bool) -> None:
        super().__init__(_LOG_FORMAT, datefmt=_DATE_FORMAT)
        self.original = original or logging.Formatter()
        self.access = access

    def format(self, record: logging.LogRecord) -> str:
        message = self.original.format(record)
        timestamp = self.formatTime(record, self.datefmt)
        source = "uvicorn.access" if self.access else f"{record.module}:{record.funcName}:{record.lineno}"
        return f"{timestamp}.{int(record.msecs):03d} | {record.levelname} | pid {record.process} | {source} - {message}"


def has_custom_uvicorn_log_config() -> bool:
    """An explicit Uvicorn logging config takes precedence over CORSIA's default format."""
    return bool(os.environ.get("UVICORN_LOG_CONFIG")) or any(
        arg == "--log-config" or arg.startswith("--log-config=") for arg in sys.argv[1:]
    )


def configure_uvicorn_logging() -> None:
    """Add trace context to Uvicorn's default handlers without changing levels."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, _TraceFormatter):
                handler.setFormatter(_TraceFormatter(handler.formatter, access=name == "uvicorn.access"))


class _StderrHandler(logging.StreamHandler):
    """The process's idempotent standard-library stderr handler."""


def configure_stdlib_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(handler, _StderrHandler) for handler in root.handlers):
        return
    handler = _StderrHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
