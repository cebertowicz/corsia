"""Process-wide redaction for MCP sign-in secrets.

Every secret an OAuth sign-in touches (access and refresh tokens, authorization
codes, PKCE verifiers, a registered client secret) is registered here the moment
it arrives, before anything parses it, because the SDK and HTTP libraries log
tracebacks around token parsing. Registered values are scrubbed from the stdlib
and loguru log streams, and from every string :func:`redact` is given. Values
are kept in memory only, never written anywhere: each stored grant's secrets for
as long as it is stored (:func:`hold`), and a bounded set of recent others.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import OrderedDict

from loguru import logger

MARK = "[REDACTED]"
# A short value would redact ordinary words; real tokens, codes and verifiers are longer.
_MIN_LENGTH = 8
_MAX_VALUES = 256

_lock = threading.Lock()
# Passing values (states, verifiers, codes, ended grants): the most recently used last, the oldest dropped.
_recent: OrderedDict[str, None] = OrderedDict()
# The secrets of each app's stored grant, kept for as long as the grant is: never dropped for newer values.
_held: dict[str, tuple[str, ...]] = {}
_values: list[str] = []
_installed = False


def _usable(values: tuple[object, ...]) -> list[str]:
    return [value for value in values if isinstance(value, str) and len(value) >= _MIN_LENGTH]


def _refresh_values() -> None:
    _values[:] = sorted({*_recent, *(value for held in _held.values() for value in held)}, key=len, reverse=True)


def _remember(values: list[str]) -> None:
    for value in values:
        _recent[value] = None
        _recent.move_to_end(value)
    while len(_recent) > _MAX_VALUES:
        _recent.popitem(last=False)


def register(*values: object) -> None:
    """Remember secret values so no log or message built later can carry them. A repeat counts as new."""
    with _lock:
        _remember(_usable(values))
        _refresh_values()


def hold(app_id: str, *values: object) -> None:
    """Redact the secrets of ``app_id``'s stored grant for as long as it is stored, whatever comes after."""
    with _lock:
        released = [value for value in _held.get(app_id, ()) if value not in values]
        _held[app_id] = tuple(_usable(values))
        _remember(released)
        _refresh_values()


def release(app_id: str) -> None:
    """``app_id``'s grant ended: its secrets stay redacted as recent values, no longer held."""
    with _lock:
        _remember(list(_held.pop(app_id, ())))
        _refresh_values()


def forget(*values: object) -> None:
    """Stop redacting values that no longer exist (tests; an ended grant keeps redacting)."""
    with _lock:
        for value in values:
            if isinstance(value, str):
                _recent.pop(value, None)
        for app_id, held in list(_held.items()):
            _held[app_id] = tuple(value for value in held if value not in values)
        _refresh_values()


def redact(text: str) -> str:
    """``text`` with every registered secret replaced."""
    with _lock:
        known = list(_values)
    for value in known:
        if value in text:
            text = text.replace(value, MARK)
    return text


def registered() -> list[str]:
    """The values currently redacted. For tests that prove a secret was registered."""
    with _lock:
        return list(_values)


def _exception_text(error: BaseException) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _values:
            return True
        message = record.getMessage()
        clean = redact(message)
        if record.exc_info and record.exc_info[1] is not None:
            detail = _exception_text(record.exc_info[1])
            if redact(detail) != detail:
                # The traceback quotes a secret: keep only the class, never the text.
                clean += f"\n{type(record.exc_info[1]).__name__}"
                record.exc_info = record.exc_text = None
        elif record.exc_text and redact(record.exc_text) != record.exc_text:
            record.exc_text = "exception text withheld"
        if clean != message:
            record.msg, record.args = clean, ()
        return True


_filter = _RedactingFilter()


def _loguru_patch(record) -> None:
    if not _values:
        return
    record["message"] = redact(record["message"])
    exception = record["exception"]
    if exception is not None and exception.value is not None:
        detail = _exception_text(exception.value)
        if redact(detail) != detail:
            record["message"] += f"\n{type(exception.value).__name__}"
            record["exception"] = None
    record["extra"] = {key: redact(value) if isinstance(value, str) else value for key, value in record["extra"].items()}


def install() -> None:
    """Attach the scrubbing filters to the root logger, its handlers and loguru. Idempotent."""
    global _installed
    logging.getLogger().addFilter(_filter)
    for handler in logging.getLogger().handlers:
        if _filter not in handler.filters:
            handler.addFilter(_filter)
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(("mcp", "httpcore", "httpx", "tools.mcp", "connections.app")):
            logging.getLogger(name).addFilter(_filter)
    if not _installed:
        logger.configure(patcher=_loguru_patch)
        _installed = True


def attach_handler(handler: logging.Handler) -> None:
    """Scrub records on a handler added after :func:`install` (tests; late log sinks)."""
    if _filter not in handler.filters:
        handler.addFilter(_filter)
