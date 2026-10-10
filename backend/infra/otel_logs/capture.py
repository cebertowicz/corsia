"""Copy CORSIA's own log records onto a bounded, never-blocking queue.

The loguru sink (INFO and above) and the root stdlib handler (WARNING and
above) run synchronously wherever a log call happens, including the event
loop, so they only copy fields and offer the record. A full queue drops and
counts the record; the sinks never block, raise or log.

Feedback is impossible by construction: records from this package's loggers,
from the writer thread and from a re-entrant emit are refused. ``uvicorn.access``
is refused because request lines are noise at this level.

``backend.log`` is the process's redirected stderr. Capture never removes or
reconfigures an existing sink. Attaching a root handler would silence Python's
``logging.lastResort``, so capture attaches ``lastResort`` itself when it was
the handler in effect, and detaches exactly the objects it attached.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loguru import Message, Record

QUEUE_MAX_RECORDS = 2_000
QUEUE_MAX_BYTES = 8 * 1024 * 1024
TEXT_MAX_BYTES = 8 * 1024
TRACEBACK_LIMIT = 30

_PIPELINE_PREFIX = "infra.otel_logs"
_EXCLUDED_LOGGERS = ("uvicorn.access",)
# uvicorn's default log config sets ``propagate=False`` on ``uvicorn``, so
# ``uvicorn.error`` never reaches root; the handler is attached there too.
_NON_PROPAGATING_LOGGERS = ("uvicorn",)
_SEEN_ATTR = "_corsia_log_captured"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_ROOT.parent
_TRUNCATED = " …[truncated]"

#: OpenTelemetry severity numbers for the level names CORSIA uses.
SEVERITY_NUMBERS = {
    "TRACE": 1, "DEBUG": 5, "INFO": 9, "SUCCESS": 10,
    "WARNING": 13, "ERROR": 17, "CRITICAL": 21,
}


@dataclass(frozen=True, slots=True)
class CapturedRecord:
    """One log record as the store keeps it."""

    ts_ns: int
    severity_number: int
    severity_text: str
    logger: str
    body: str
    exception: str | None
    function: str | None
    file_path: str | None
    line: int | None

    @property
    def size(self) -> int:
        """Approximate stored bytes, for the queue and batch byte bounds."""
        return (
            len(self.body) + len(self.exception or "") + len(self.logger)
            + len(self.function or "") + len(self.file_path or "") + 64
        )


class CaptureQueue:
    """A deque bounded by record count and bytes; ``offer`` never waits.

    The lock guards an O(1) append or a bounded drain, so it is never held
    across I/O.
    """

    def __init__(
        self,
        max_records: int = QUEUE_MAX_RECORDS,
        max_bytes: int = QUEUE_MAX_BYTES,
    ) -> None:
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._items: deque[CapturedRecord] = deque()
        self._bytes = 0
        self._ready = threading.Event()
        self.dropped_full = 0

    def offer(self, record: CapturedRecord) -> bool:
        """Enqueue ``record``, or drop and count it when the queue is full."""
        size = record.size
        with self._lock:
            if (
                len(self._items) >= self._max_records
                or self._bytes + size > self._max_bytes
            ):
                self.dropped_full += 1
                return False
            self._items.append(record)
            self._bytes += size
        self._ready.set()
        return True

    def drain(self, max_rows: int, max_bytes: int) -> list[CapturedRecord]:
        """Pop up to ``max_rows`` records and about ``max_bytes`` bytes, oldest first."""
        batch: list[CapturedRecord] = []
        taken = 0
        with self._lock:
            while self._items and len(batch) < max_rows:
                size = self._items[0].size
                if batch and taken + size > max_bytes:
                    break
                batch.append(self._items.popleft())
                self._bytes -= size
                taken += size
        return batch

    def wait(self, timeout: float) -> None:
        """Block the writer until a record arrives or ``timeout`` passes."""
        self._ready.wait(timeout)
        self._ready.clear()

    def wake(self) -> None:
        """Release a waiting writer without a record (used on stop)."""
        self._ready.set()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _cut(text: str, limit: int = TEXT_MAX_BYTES) -> str:
    """Keep the first ``limit`` UTF-8 bytes of ``text``, marking a cut."""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore") + _TRUNCATED


def _format_exception(exc_type: Any, exc_value: Any, exc_tb: Any) -> str | None:
    if exc_type is None and exc_value is None:
        return None
    try:
        text = "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb, limit=TRACEBACK_LIMIT)
        )
    except Exception:
        text = f"{getattr(exc_type, '__name__', exc_type)}: {exc_value!r}"
    return _cut(text.rstrip("\n"))


def _relative_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(_REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return Path(path).name


def _severity(name: str, number: int) -> tuple[int, str]:
    """Map a level to its OTel number, falling back on the numeric level."""
    if name in SEVERITY_NUMBERS:
        return SEVERITY_NUMBERS[name], name
    for floor, mapped in ((50, "CRITICAL"), (40, "ERROR"), (30, "WARNING"), (25, "SUCCESS")):
        if number >= floor:
            return SEVERITY_NUMBERS[mapped], mapped
    return SEVERITY_NUMBERS["INFO"], "INFO"


class _StdlibHandler(logging.Handler):
    """Root handler that forwards WARNING+ stdlib records to the capture."""

    def __init__(self, capture: LogCapture) -> None:
        super().__init__(logging.WARNING)
        self._capture = capture

    def emit(self, record: logging.LogRecord) -> None:
        """Hand the record to the capture once; never raises."""
        # A record from a propagating ``uvicorn`` reaches this handler twice.
        if getattr(record, _SEEN_ATTR, False):
            return
        setattr(record, _SEEN_ATTR, True)
        self._capture.capture_stdlib(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """Never print a traceback for a capture failure."""


class LogCapture:
    """The two sinks, their exclusion rules and their attach/detach state."""

    def __init__(self, queue: CaptureQueue) -> None:
        self.queue = queue
        self._local = threading.local()
        self._excluded_thread: int | None = None
        self._loguru_id: int | None = None
        self._handler: _StdlibHandler | None = None
        self._attached_last_resort: logging.Handler | None = None
        self.dropped_errors = 0

    @property
    def attached(self) -> bool:
        """Whether the sinks are currently attached."""
        return self._handler is not None

    def exclude_thread(self, ident: int | None) -> None:
        """Refuse every record emitted on ``ident`` (the writer thread)."""
        self._excluded_thread = ident

    def admits(self, logger_name: str) -> bool:
        """Return whether a record from ``logger_name`` on this thread may be captured."""
        if getattr(self._local, "busy", False):
            return False
        if self._excluded_thread is not None and threading.get_ident() == self._excluded_thread:
            return False
        if logger_name == _PIPELINE_PREFIX or logger_name.startswith(_PIPELINE_PREFIX + "."):
            return False
        return not any(
            logger_name == name or logger_name.startswith(name + ".")
            for name in _EXCLUDED_LOGGERS
        )

    def _offer(self, build) -> None:
        self._local.busy = True
        try:
            self.queue.offer(build())
        except Exception:
            self.dropped_errors += 1
        finally:
            self._local.busy = False

    def _loguru_filter(self, record: Record) -> bool:
        return self.admits(record.get("name") or "")

    def _loguru_sink(self, message: Message) -> None:
        record = message.record

        def build() -> CapturedRecord:
            level = record["level"]
            number, text = _severity(level.name, level.no)
            exc = record["exception"]
            return CapturedRecord(
                ts_ns=int(record["time"].timestamp() * 1_000_000_000),
                severity_number=number,
                severity_text=text,
                logger=record["name"] or "",
                body=_cut(str(record["message"])),
                exception=_format_exception(*exc) if exc is not None else None,
                function=record["function"],
                file_path=_relative_path(record["file"].path),
                line=record["line"],
            )

        self._offer(build)

    def capture_stdlib(self, record: logging.LogRecord) -> None:
        """Offer one stdlib record; the stdlib handler's ``emit`` target."""
        if not self.admits(record.name or ""):
            return

        def build() -> CapturedRecord:
            try:
                body = record.getMessage()
            except Exception:
                body = str(record.msg)
            number, text = _severity(record.levelname, record.levelno)
            exc = record.exc_info if isinstance(record.exc_info, tuple) else None
            return CapturedRecord(
                ts_ns=int(record.created * 1_000_000_000),
                severity_number=number,
                severity_text=text,
                logger=record.name or "",
                body=_cut(body),
                exception=_format_exception(*exc) if exc else None,
                function=record.funcName,
                file_path=_relative_path(record.pathname),
                line=record.lineno,
            )

        self._offer(build)

    def attach(self) -> None:
        """Add both sinks without changing any existing output (R1)."""
        from loguru import logger

        if self.attached:
            return
        root = logging.getLogger()
        last_resort = logging.lastResort
        # lastResort prints only when no handler exists; with none on root it
        # was in effect, so keep it in effect once our handler is added.
        if last_resort is not None and not root.handlers:
            root.addHandler(last_resort)
            self._attached_last_resort = last_resort
        self._handler = _StdlibHandler(self)
        root.addHandler(self._handler)
        for name in _NON_PROPAGATING_LOGGERS:
            logging.getLogger(name).addHandler(self._handler)
        self._loguru_id = logger.add(
            self._loguru_sink,
            level="INFO",
            format="{message}",
            filter=self._loguru_filter,
            catch=True,
            enqueue=False,
        )

    def detach(self) -> None:
        """Remove exactly the sink and handlers ``attach`` added (R1)."""
        from loguru import logger

        if self._loguru_id is not None:
            try:
                logger.remove(self._loguru_id)
            except ValueError:
                pass
            self._loguru_id = None
        root = logging.getLogger()
        if self._handler is not None:
            root.removeHandler(self._handler)
            for name in _NON_PROPAGATING_LOGGERS:
                logging.getLogger(name).removeHandler(self._handler)
            self._handler = None
        if self._attached_last_resort is not None:
            root.removeHandler(self._attached_last_resort)
            self._attached_last_resort = None
