"""Keep loguru tracebacks free of frame variables, which can hold keys and tokens.

Loguru's default stderr handler formats an exception with ``diagnose=True``: every
traceback line is annotated with the values of the variables on it. The server's
stderr is ``backend.log``, a file people paste into bug reports, so a key held by a
failing frame would land there. The traceback itself is kept; only the values go.
"""

from __future__ import annotations

import logging
import sys
from copy import deepcopy

from loguru import logger
from uvicorn.config import LOGGING_CONFIG

_DEFAULT_HANDLER_ID = 0
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


def drop_traceback_locals() -> None:
    """Format loguru's stderr handler without traceback locals. Idempotent."""
    try:
        logger.remove(_DEFAULT_HANDLER_ID)
    except ValueError:
        return  # already replaced, or removed by an entrypoint that set its own sinks
    logger.add(
        sys.stderr,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | pid {process.id} | {module}:{function}:{line} - {message}",
    )
