"""Opt-in CPU attribution by thread for diagnosing a slow CORSIA process.

The sampler is observational: it attributes native-thread CPU deltas to compact,
privacy-safe Python stack signatures without opening a database connection or
acquiring application locks. It runs only when the ``cpu_profiler`` feature flag
is saved on or CORSIA_CPU_SAMPLER=1 is set, because its sampling thread is not
free and has crashed older interpreters. ``apply_cpu_profiler`` is the single
entry point that makes the running process match the flag.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType

from infra.thread_cpu import ThreadCpuUnavailable, thread_cpu_seconds

log = logging.getLogger(__name__)

ENABLE_ENV = "CORSIA_CPU_SAMPLER"
DEFAULT_INTERVAL_SECONDS = 0.1
SUMMARY_SECONDS = 60.0
STOP_TIMEOUT_SECONDS = 1.0
MAX_SUMMARY_CHARS = 8_192
SUMMARY_ENTRY_COUNT = 15
THREAD_ENTRY_CHARS = 100
STACK_ENTRY_CHARS = 320
_BACKEND_PREFIX = f"{Path(__file__).resolve().parents[1]}{os.sep}"
_STDLIB_PREFIX = f"{Path(os.__file__).resolve().parent}{os.sep}"
_lifecycle_lock = threading.Lock()
_sampler: CpuSampler | None = None
_unavailable_logged = False


def sampler_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether process CPU sampling is enabled; it is off unless explicitly set to 1."""
    env = os.environ if environ is None else environ
    return env.get(ENABLE_ENV) == "1"


def _short_path(filename: str) -> str:
    """Render backend paths relatively without exposing external absolute paths."""
    if filename.startswith(_BACKEND_PREFIX):
        return filename.removeprefix(_BACKEND_PREFIX)
    parts = Path(filename).parts
    if "site-packages" in parts:
        return "/".join(parts[parts.index("site-packages") :])
    if filename.startswith(_STDLIB_PREFIX):
        return f"stdlib/{Path(filename).name}"
    return f"external/{Path(filename).name}"


def _thread_group(thread_name: str) -> str:
    """Return a stable thread label without worker-instance cardinality."""
    return re.sub(r"\d+", "N", thread_name)


def _is_idle_frame(frame: FrameType) -> bool:
    """Identify a worker sampled while blocked on its work queue."""
    code = frame.f_code
    return _short_path(code.co_filename) == "stdlib/thread.py" and code.co_name == "_worker"


def _signature(frame: FrameType, thread_group: str) -> str:
    """Return six innermost frames without locals, arguments, or values."""
    frames: list[str] = []
    current: FrameType | None = frame
    while current is not None and len(frames) < 6:
        code = current.f_code
        frames.append(f"{_short_path(code.co_filename)}:{code.co_name}:{current.f_lineno}")
        current = current.f_back
    sample = "idle" if _is_idle_frame(frame) else "active"
    return f"thread_group={thread_group} sample={sample} stack={' <- '.join(frames)}"


def _unmapped_signature(reason: str) -> str:
    """Represent native CPU even when its Python frame is unavailable."""
    return f"thread_group=unmapped-native-N sample=unmapped stack={reason}"


def _ellipsize(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


class CpuSampler:
    """Attribute process native-thread CPU deltas to Python stack snapshots."""

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        summary_seconds: float = SUMMARY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        current_frames: Callable[[], dict[int, FrameType]] = sys._current_frames,
        threads: Callable[[], Sequence[threading.Thread]] = threading.enumerate,
        thread_cpu_reader: Callable[[], dict[int, float]] = thread_cpu_seconds,
        logger: logging.Logger = log,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.summary_seconds = summary_seconds
        self._clock = clock
        self._current_frames = current_frames
        self._threads = threads
        self._thread_cpu_reader = thread_cpu_reader
        self._logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._thread_native_id: int | None = None
        self._previous_cpu_seconds: dict[int, float] = {}
        self._thread_cpu_seconds: dict[str, float] = {}
        self._stack_cpu_seconds: dict[str, float] = {}
        self._process_cpu_seconds = 0.0
        self._sampler_cpu_seconds = 0.0
        self._max_tick_lag_seconds = 0.0
        self._window_started_at = self._clock()

    def start(self) -> None:
        """Start once, or reuse the active sampler thread."""
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cpu-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Request termination and wait only the bounded diagnostic timeout."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        """Whether the sampler daemon is currently live."""
        return self._thread is not None and self._thread.is_alive()

    def sample_once(self) -> int:
        """Attribute one native-thread CPU-delta snapshot and return entries added."""
        try:
            current_cpu_seconds = self._thread_cpu_reader()
        except (ThreadCpuUnavailable, OSError) as error:
            self._logger.warning("cpu sampler: native thread snapshot failed: %s", error)
            return 0

        python_threads = {
            thread.native_id: (thread.ident, _thread_group(thread.name))
            for thread in self._threads()
            if thread.native_id is not None and thread.ident is not None
        }
        frames = self._current_frames()
        added = 0
        for native_id, cpu_seconds in current_cpu_seconds.items():
            previous = self._previous_cpu_seconds.get(native_id)
            if previous is None:
                continue
            delta = cpu_seconds - previous
            if delta <= 0:
                continue
            self._process_cpu_seconds += delta
            python_thread = python_threads.get(native_id)
            if native_id == self._thread_native_id:
                self._sampler_cpu_seconds += delta
            if python_thread is None:
                thread_label = "unmapped-native-N"
                signature = _unmapped_signature("python-unmapped")
            else:
                ident, thread_group = python_thread
                thread_label = thread_group
                frame = frames.get(ident)
                signature = (
                    _signature(frame, thread_group)
                    if frame is not None
                    else _unmapped_signature("python-frame-unavailable")
                )
            self._thread_cpu_seconds[thread_label] = self._thread_cpu_seconds.get(thread_label, 0.0) + delta
            self._stack_cpu_seconds[signature] = self._stack_cpu_seconds.get(signature, 0.0) + delta
            added += 1
        self._previous_cpu_seconds = current_cpu_seconds
        return added

    def emit_summary(self, window_seconds: float | None = None) -> None:
        """Log and reset one bounded CPU-attribution window."""
        if window_seconds is None:
            window_seconds = max(0.0, self._clock() - self._window_started_at)
        top_threads = "; ".join(
            f"{seconds:.6f}s {_ellipsize(label, THREAD_ENTRY_CHARS)}"
            for label, seconds in sorted(
                self._thread_cpu_seconds.items(), key=lambda item: item[1], reverse=True
            )[:SUMMARY_ENTRY_COUNT]
        ) or "none"
        top_stack_items = sorted(
            self._stack_cpu_seconds.items(), key=lambda item: item[1], reverse=True
        )[:SUMMARY_ENTRY_COUNT]
        top_stack_seconds = sum(seconds for _, seconds in top_stack_items)
        top_stacks = "; ".join(
            f"{seconds:.6f}s {_ellipsize(signature, STACK_ENTRY_CHARS)}"
            for signature, seconds in top_stack_items
        ) or "none"
        other_stack_seconds = max(0.0, self._process_cpu_seconds - top_stack_seconds)
        message = (
            "cpu sampler: "
            f"process_cpu_seconds={self._process_cpu_seconds:.6f} "
            f"window_seconds={window_seconds:.3f} "
            f"sampler_cpu_seconds={self._sampler_cpu_seconds:.6f} "
            f"max_tick_lag_seconds={self._max_tick_lag_seconds:.6f} "
            f"top_threads=[{top_threads}] "
            f"top_stacks=[{top_stacks}; other={other_stack_seconds:.6f}s]"
        )
        # WARNING, not INFO: the served process leaves the stdlib root logger at
        # WARNING (uvicorn configures only its own loggers), and both the stderr
        # fallback and the OTel capture handler accept WARNING and above, so an
        # INFO summary is dropped before it reaches any sink.
        self._logger.warning("%s", _ellipsize(message, MAX_SUMMARY_CHARS))
        self._thread_cpu_seconds = {}
        self._stack_cpu_seconds = {}
        self._process_cpu_seconds = 0.0
        self._sampler_cpu_seconds = 0.0
        self._max_tick_lag_seconds = 0.0
        self._window_started_at = self._clock()

    def _run(self) -> None:
        self._thread_ident = threading.get_ident()
        self._thread_native_id = threading.get_native_id()
        started_at = self._clock()
        self._window_started_at = started_at
        next_tick = started_at + self.interval_seconds
        next_summary = started_at + self.summary_seconds
        while True:
            delay = max(0.0, next_tick - self._clock())
            if self._stop.wait(delay):
                return
            now = self._clock()
            self._max_tick_lag_seconds = max(self._max_tick_lag_seconds, now - next_tick)
            self.sample_once()
            next_tick += self.interval_seconds
            while next_tick <= now:
                next_tick += self.interval_seconds
            if now >= next_summary:
                self.emit_summary(now - self._window_started_at)
                while next_summary <= now:
                    next_summary += self.summary_seconds


def start_cpu_sampler(environ: Mapping[str, str] | None = None, *, enabled: bool = False) -> bool:
    """Start the process sampler when enabled by flag or environment; never block startup."""
    global _sampler, _unavailable_logged
    env = os.environ if environ is None else environ
    with _lifecycle_lock:
        if _sampler is not None:
            return _sampler.is_running()
        if not (enabled or sampler_enabled(env)):
            return False
        try:
            thread_cpu_seconds()
        except Exception as error:
            if not _unavailable_logged:
                log.warning("cpu sampler: per-thread CPU unavailable; not starting: %s", error)
                _unavailable_logged = True
            return False
        try:
            sampler = CpuSampler()
            sampler.start()
        except Exception as error:
            log.warning("cpu sampler failed to start: %s", error)
            return False
        _sampler = sampler
        return True


def cpu_sampler_running() -> bool:
    """Whether the process sampler is live right now."""
    with _lifecycle_lock:
        return _sampler is not None and _sampler.is_running()


def stop_cpu_sampler() -> None:
    """Stop the sampler before logging infrastructure is torn down."""
    global _sampler
    with _lifecycle_lock:
        sampler = _sampler
    if sampler is None:
        return
    sampler.stop()
    with _lifecycle_lock:
        if _sampler is sampler and not sampler.is_running():
            _sampler = None


def apply_cpu_profiler(
    enabled: bool, environ: Mapping[str, str] | None = None
) -> bool:
    """Make the running sampler match the saved flag; the env override keeps it on.

    Blocking (stopping joins the sampler thread): call from a worker thread.
    """
    env = os.environ if environ is None else environ
    if enabled or sampler_enabled(env):
        return start_cpu_sampler(env, enabled=True)
    stop_cpu_sampler()
    return False
