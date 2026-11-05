"""Keep loguru tracebacks free of frame variables, which can hold keys and tokens.

Loguru's default stderr handler formats an exception with ``diagnose=True``: every
traceback line is annotated with the values of the variables on it. The server's
stderr is ``backend.log``, a file people paste into bug reports, so a key held by a
failing frame would land there. The traceback itself is kept; only the values go.
"""

from __future__ import annotations

import sys

from loguru import logger

_DEFAULT_HANDLER_ID = 0


def drop_traceback_locals() -> None:
    """Replace loguru's default stderr handler with the same one minus ``diagnose``. Idempotent."""
    try:
        logger.remove(_DEFAULT_HANDLER_ID)
    except ValueError:
        return  # already replaced, or removed by an entrypoint that set its own sinks
    logger.add(sys.stderr, diagnose=False)
