"""Capture of CORSIA's own log records: binding, drops, feedback, R1, backend.log."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from loguru import logger

from infra.otel_logs.capture import CaptureQueue, CapturedRecord, LogCapture

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def uvicorn_loggers():
    """uvicorn loggers at their unconfigured levels.

    Any test that starts a uvicorn server leaves its log level on these global
    loggers (``log_level="error"`` drops WARNING at the logger itself).
    ``setLevel`` also clears the ``isEnabledFor`` cache.
    """
    loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error")]
    saved = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.NOTSET)
    try:
        yield loggers[0]
    finally:
        for lg, level in zip(loggers, saved):
            lg.setLevel(level)


@pytest.fixture
def capture(monkeypatch):
    """An attached capture on a root logger with no handlers, as in production."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    cap = LogCapture(CaptureQueue())
    cap.attach()
    try:
        yield cap
    finally:
        cap.detach()


def _drained(cap: LogCapture) -> list[CapturedRecord]:
    return cap.queue.drain(10_000, 1 << 40)


def _record(body: str = "x") -> CapturedRecord:
    return CapturedRecord(1, 9, "INFO", "t", body, None, None, None, None)


def test_capture_binds_loguru_and_stdlib_fields(capture):
    logger.debug("loguru debug stays out")
    logger.info("loguru info {}", 7)
    logging.getLogger("pkg.mod").info("stdlib info stays out")
    logging.getLogger("pkg.mod").warning("stdlib warning %s", "w")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("loguru failed")
        logging.getLogger("pkg.mod").exception("stdlib failed")

    records = _drained(capture)
    assert [r.body for r in records] == [
        "loguru info 7", "stdlib warning w", "loguru failed", "stdlib failed",
    ]
    info, warning, loguru_exc, stdlib_exc = records
    assert (info.severity_text, info.severity_number) == ("INFO", 9)
    assert info.logger == __name__
    assert info.file_path == "backend/tests/test_otel_logs_capture.py"
    assert info.function == "test_capture_binds_loguru_and_stdlib_fields"
    assert isinstance(info.line, int) and info.exception is None
    assert abs(info.ts_ns - time.time_ns()) < 60 * 1_000_000_000
    assert (warning.severity_text, warning.logger) == ("WARNING", "pkg.mod")
    for record in (loguru_exc, stdlib_exc):
        assert record.severity_text == "ERROR"
        assert record.exception.startswith("Traceback")
        assert record.exception.endswith("ValueError: boom")


def test_capture_excludes_uvicorn_access_and_cuts_long_text(uvicorn_loggers, capture):
    logging.getLogger("uvicorn.access").warning("GET / 200")
    logging.getLogger("uvicorn.error").warning("kept")
    logger.info("y" * 20_000)
    records = _drained(capture)
    assert [r.logger for r in records] == ["uvicorn.error", __name__]
    assert len(records[1].body.encode()) < 8 * 1024 + 32
    assert records[1].body.endswith("[truncated]")


def test_capture_keeps_uvicorn_error_once_under_uvicorn_log_config(
    uvicorn_loggers, monkeypatch,
):
    """uvicorn's default config stops ``uvicorn`` propagating to root."""
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    uvicorn_logger = uvicorn_loggers
    monkeypatch.setattr(uvicorn_logger, "handlers", [logging.NullHandler()])
    for propagate in (False, True):
        monkeypatch.setattr(uvicorn_logger, "propagate", propagate)
        cap = LogCapture(CaptureQueue())
        cap.attach()
        try:
            logging.getLogger("uvicorn.error").warning("server error")
        finally:
            cap.detach()
        assert [r.logger for r in _drained(cap)] == ["uvicorn.error"]
    assert uvicorn_logger.handlers[0].__class__ is logging.NullHandler
    assert len(uvicorn_logger.handlers) == 1


def test_full_queue_drops_and_counts_without_blocking():
    queue = CaptureQueue(max_records=2, max_bytes=1 << 20)
    started = time.monotonic()
    results = [queue.offer(_record()) for _ in range(5)]
    assert time.monotonic() - started < 0.1
    assert results == [True, True, False, False, False]
    assert queue.dropped_full == 3 and len(queue) == 2

    by_bytes = CaptureQueue(max_records=100, max_bytes=_record("a" * 100).size * 2)
    assert [by_bytes.offer(_record("a" * 100)) for _ in range(3)] == [True, True, False]
    assert by_bytes.dropped_full == 1


def test_full_queue_drops_through_the_real_sinks(monkeypatch):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    cap = LogCapture(CaptureQueue(max_records=3))
    cap.attach()
    try:
        for n in range(10):
            logger.warning("w{}", n)
    finally:
        cap.detach()
    assert len(cap.queue) == 3 and cap.queue.dropped_full == 7


def test_no_feedback_from_pipeline_loggers_writer_thread_or_reentry(capture):
    logger.patch(lambda r: r.update(name="infra.otel_logs.writer")).warning("own")
    logging.getLogger("infra.otel_logs.store").error("own stdlib")

    def on_writer_thread():
        capture.exclude_thread(threading.get_ident())
        logger.error("from the writer thread")
        logging.getLogger("pkg").error("stdlib from the writer thread")

    worker = threading.Thread(target=on_writer_thread)
    worker.start()
    worker.join()

    class LogsWhenFormatted:
        def __str__(self) -> str:
            logging.getLogger("pkg.inner").error("inner emit")
            return "outer"

    # A log call made while capture formats a record is refused; other
    # handlers formatting the same record outside capture are not ours.
    record = logging.LogRecord(
        "pkg.outer", logging.ERROR, __file__, 1, "%s", (LogsWhenFormatted(),), None,
    )
    capture.capture_stdlib(record)
    assert [r.body for r in _drained(capture)] == ["outer"]


def test_r1_attaches_last_resort_only_when_it_was_in_effect(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    loguru_before = set(logger._core.handlers)
    cap = LogCapture(CaptureQueue())
    cap.attach()
    assert root.handlers[0] is logging.lastResort and len(root.handlers) == 2
    cap.detach()
    assert root.handlers == []
    assert set(logger._core.handlers) == loguru_before

    existing = logging.StreamHandler()
    monkeypatch.setattr(root, "handlers", [existing])
    cap = LogCapture(CaptureQueue())
    cap.attach()
    assert logging.lastResort not in root.handlers and root.handlers[0] is existing
    cap.detach()
    assert root.handlers == [existing]
    cap.detach()  # idempotent
    assert root.handlers == [existing]


_EMIT_SCRIPT = r"""
import logging, sys
from loguru import logger
from infra.otel_logs.capture import CaptureQueue, LogCapture
cap = None
if sys.argv[1] == "on":
    cap = LogCapture(CaptureQueue())
    cap.attach()
logger.debug("loguru debug")
logger.info("loguru info {}", 1)
logger.warning("loguru warning")
logging.getLogger("pkg.mod").info("stdlib info")
logging.getLogger("pkg.mod").warning("stdlib warning %s", 2)
logging.getLogger("uvicorn.access").warning("access line")
try:
    1 / 0
except ZeroDivisionError:
    logging.getLogger("pkg").exception("stdlib exc")
    logger.exception("loguru exc")
if cap is not None:
    cap.detach()
    logging.getLogger("pkg.after").warning("after detach")
    print(len(cap.queue))
else:
    logging.getLogger("pkg.after").warning("after detach")
"""

# Unanchored: loguru colorizes under CI (GITHUB_ACTIONS), so an ANSI escape
# precedes each timestamp there.
_LOGURU_TIME = re.compile(rb"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")


def _stderr(mode: str, tmp_path: Path) -> tuple[bytes, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "CORSIA_WORKSPACE": str(tmp_path / "ws"),
        "CORSIA_STATE_DIR": str(tmp_path / "state"),
        "CORSIA_TEST_MODE": "1",
        "CORSIA_ANALYTICS_DISABLE": "1",
    }
    done = subprocess.run(
        [sys.executable, "-c", _EMIT_SCRIPT, mode],
        cwd=BACKEND, env=env, capture_output=True, timeout=60, check=True,
    )
    return _LOGURU_TIME.sub(b"<time>", done.stderr), done.stdout.decode().strip()


def test_backend_log_stream_is_byte_identical_with_capture(tmp_path):
    without, _ = _stderr("off", tmp_path)
    with_capture, captured = _stderr("on", tmp_path)
    assert b"stdlib warning 2" in without and b"access line" in without
    assert b"loguru exc" in without and b"ZeroDivisionError" in without
    assert with_capture == without
    assert int(captured) == 5
