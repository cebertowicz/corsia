"""Traceable formatting for backend and Uvicorn log records."""

from __future__ import annotations

import logging
import sys
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import ColourizedFormatter

LOGURU_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | pid {process.id} | {module}:{function}:{line} - {message}"
_LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)s | pid %(process)d | %(module)s:%(funcName)s:%(lineno)d - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_UVICORN_FORMATTERS = (("uvicorn", "default"), ("uvicorn.error", "default"), ("uvicorn.access", "access"))


def uvicorn_log_config() -> dict:
    """Preserve Uvicorn's handlers and levels while making its lines traceable."""
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"].update(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    config["formatters"]["access"].update(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)s | pid %(process)d | uvicorn.access - %(client_addr)s - \"%(request_line)s\" %(status_code)s",
        datefmt=_DATE_FORMAT,
    )
    return config


def configure_uvicorn_logging() -> None:
    """Give handlers Uvicorn built with its stock formats the traceable ones, keeping levels and handlers.

    A logging config given to Uvicorn by flag, environment or code brings its own formats;
    its handlers and the root stderr handler are then left as configured.
    """
    traced = uvicorn_log_config()["formatters"]
    custom = False
    for logger_name, key in _UVICORN_FORMATTERS:
        for handler in logging.getLogger(logger_name).handlers:
            formatter = handler.formatter
            if not isinstance(formatter, ColourizedFormatter):
                custom = True
            elif formatter._fmt == LOGGING_CONFIG["formatters"][key]["fmt"]:
                handler.setFormatter(type(formatter)(
                    fmt=traced[key]["fmt"], datefmt=_DATE_FORMAT, use_colors=formatter.use_colors,
                ))
            elif formatter._fmt != traced[key]["fmt"]:
                custom = True
    if not custom:
        configure_stdlib_logging()


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
