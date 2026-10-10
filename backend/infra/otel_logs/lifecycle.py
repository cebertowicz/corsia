"""Start and stop the CORSIA log pipeline, and report its state.

R2: capture is on for production and the port-3001 dev instance and off for
pytest and worktree test instances. The existing runtime signal that separates
them is ``CORSIA_TEST_MODE=1``: ``backend/conftest.py`` sets it, the test
environment requires it for every worktree backend, and the supported
launchers (``scripts/corsia.d/runtime.sh``, ``service.sh``) strip it. A test
may opt in with ``CORSIA_LOG_CAPTURE_TEST_ENABLE=1`` on an isolated instance,
the same shape as the GitHub notifications poller's test opt-in.

Order: the writer claims the store before the sinks attach, and the sinks
detach before the writer drains, so no record is captured without a writer.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from infra.otel_logs.capture import CaptureQueue, LogCapture
from infra.otel_logs.store import LogStore, stderr_line
from infra.otel_logs.writer import LogWriter

TEST_ENABLE_ENV = "CORSIA_LOG_CAPTURE_TEST_ENABLE"


@dataclass
class _Pipeline:
    capture: LogCapture
    writer: LogWriter


_lock = threading.Lock()
_pipeline: _Pipeline | None = None
_state = "not_started"


def capture_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this instance kind captures its own logs (R2)."""
    env = os.environ if environ is None else environ
    return env.get("CORSIA_TEST_MODE") != "1" or env.get(TEST_ENABLE_ENV) == "1"


def default_store_path() -> Path:
    """The instance's store file, ``$STATE_ROOT/otel_logs.db``."""
    from infra.config import OTEL_LOGS_DB_PATH

    return OTEL_LOGS_DB_PATH


def start_corsia_logs(path: Path | None = None, **store_options) -> str:
    """Start the writer, then attach capture; return the resulting state.

    Never raises: a failure leaves capture off and ``backend.log`` untouched.
    """
    global _pipeline, _state
    with _lock:
        if _pipeline is not None:
            return _state
        if not capture_enabled():
            _state = "off_test_instance"
            return _state
        try:
            store = LogStore(path or default_store_path(), **store_options)
            if not store.open():
                stderr_line("another process holds the log store writer; capture is off")
                _state = "off_writer_held"
                return _state
            queue = CaptureQueue()
            capture = LogCapture(queue)
            writer = LogWriter(store, queue, capture)
            writer.start()
            capture.attach()
        except Exception as exc:
            stderr_line(f"capture failed to start ({type(exc).__name__}): {exc}")
            _state = "off_start_failed"
            return _state
        _pipeline = _Pipeline(capture, writer)
        _state = "on"
        return _state


def stop_corsia_logs(logger=None) -> None:
    """Detach capture, then drain and close the writer; never raises."""
    global _pipeline, _state
    with _lock:
        pipeline, _pipeline = _pipeline, None
        if pipeline is None:
            return
        _state = "stopped"
    try:
        pipeline.capture.detach()
        pipeline.writer.stop()
    except Exception as exc:
        if logger is not None:
            logger.warning(f"shutdown: log capture stop failed: {exc}")


def capture_status() -> dict:
    """Return the pipeline state and its process-lifetime drop counters."""
    with _lock:
        pipeline, state = _pipeline, _state
    status: dict = {"state": state}
    if pipeline is not None:
        status["queued"] = len(pipeline.capture.queue)
        status["dropped_queue_full"] = pipeline.capture.queue.dropped_full
        status["dropped_capture_error"] = pipeline.capture.dropped_errors
        status["dropped_shutdown"] = pipeline.writer.dropped_shutdown
        status.update(pipeline.writer.store.counters.as_dict())
    return status
