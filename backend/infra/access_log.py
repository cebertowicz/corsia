"""Shared protection for Uvicorn access-log request targets."""

from __future__ import annotations

import logging


class _DropQueryStringFilter(logging.Filter):
    """Remove query strings before Uvicorn formats an access record."""

    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if not isinstance(arguments, tuple) or len(arguments) < 3:
            return True
        request_target = arguments[2]
        if not isinstance(request_target, str) or "?" not in request_target:
            return True
        record.args = (*arguments[:2], request_target.split("?", 1)[0], *arguments[3:])
        return True


def install_access_log_redaction() -> None:
    """Install one idempotent filter on Uvicorn's canonical access logger."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _DropQueryStringFilter) for item in logger.filters):
        logger.addFilter(_DropQueryStringFilter())
