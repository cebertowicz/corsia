"""Behavioral coverage for the in-process native-thread CPU sampler."""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time

import app_startup_runtime
import pytest
from infra import cpu_sampler, thread_cpu


def _busy_worker(started: threading.Event, release: threading.Event) -> None:
    started.set()
    value = 0
    while not release.is_set():
        value += 1


def _event_wait_worker(started: threading.Event, release: threading.Event) -> None:
    started.set()
    release.wait()


def _queue_wait_worker(started: threading.Event, work: queue.Queue[None]) -> None:
    started.set()
    work.get()


def _thread_cpu_seconds(sampler: cpu_sampler.CpuSampler, name: str) -> float:
    return next(
        seconds
        for label, seconds in sampler._thread_cpu_seconds.items()
        if label.startswith(name)
    )


def test_busy_thread_is_ranked_first_while_event_and_queue_waiters_use_no_cpu() -> None:
    release = threading.Event()
    queue_work: queue.Queue[None] = queue.Queue()
    busy_started, event_started, queue_started = threading.Event(), threading.Event(), threading.Event()
    busy = threading.Thread(
        target=_busy_worker, args=(busy_started, release), name="controlled-busy", daemon=True,
    )
    event_waiter = threading.Thread(
        target=_event_wait_worker, args=(event_started, release), name="event-waiter", daemon=True,
    )
    queue_waiter = threading.Thread(
        target=_queue_wait_worker, args=(queue_started, queue_work), name="queue-waiter", daemon=True,
    )
    for worker in (busy, event_waiter, queue_waiter):
        worker.start()
    assert busy_started.wait(1)
    assert event_started.wait(1)
    assert queue_started.wait(1)

    sampler = cpu_sampler.CpuSampler()
    try:
        assert sampler.sample_once() == 0
        # Let the operating system account CPU for the real busy thread while
        # both waiters remain blocked; no fabricated per-thread counters.
        time.sleep(0.2)
        assert sampler.sample_once() > 0
    finally:
        release.set()
        queue_work.put(None)
        for worker in (busy, event_waiter, queue_waiter):
            worker.join(1)

    busy_cpu = _thread_cpu_seconds(sampler, "controlled-busy")
    total_cpu = sum(sampler._thread_cpu_seconds.values())
    assert max(sampler._thread_cpu_seconds, key=sampler._thread_cpu_seconds.get).startswith("controlled-busy")
    assert busy_cpu / total_cpu > 0.5
    assert all("event-waiter" not in label for label in sampler._thread_cpu_seconds)
    assert all("queue-waiter" not in label for label in sampler._thread_cpu_seconds)
    assert any("_busy_worker" in signature for signature in sampler._stack_cpu_seconds)


def test_start_is_idempotent_and_stop_joins_within_the_requested_bound() -> None:
    sampler = cpu_sampler.CpuSampler()
    sampler.start()
    first_thread = sampler._thread
    sampler.start()
    assert sampler._thread is first_thread
    started = time.monotonic()
    sampler.stop(timeout=0.2)
    assert time.monotonic() - started < 0.2
    assert not sampler.is_running()
    sampler.stop(timeout=0.2)


def test_summary_reports_cpu_deltas_threads_stacks_sampler_and_tick_lag(caplog) -> None:
    # Production leaves the stdlib root logger at WARNING; the summary must pass it.
    caplog.set_level(logging.WARNING, logger="infra.cpu_sampler")
    sampler = cpu_sampler.CpuSampler()
    sampler._process_cpu_seconds = 1.25
    sampler._sampler_cpu_seconds = 0.05
    sampler._max_tick_lag_seconds = 0.012
    sampler._thread_cpu_seconds["worker"] = 1.2
    sampler._stack_cpu_seconds["thread_group=worker sample=active stack=backend/x.py:work:1"] = 1.2

    sampler.emit_summary(window_seconds=60)

    message = caplog.records[-1].getMessage()
    assert message == (
        "cpu sampler: process_cpu_seconds=1.250000 window_seconds=60.000 "
        "sampler_cpu_seconds=0.050000 max_tick_lag_seconds=0.012000 "
        "top_threads=[1.200000s worker] "
        "top_stacks=[1.200000s thread_group=worker sample=active "
        "stack=backend/x.py:work:1; other=0.050000s]"
    )
    assert len(message) <= cpu_sampler.MAX_SUMMARY_CHARS
    assert not sampler._thread_cpu_seconds
    assert not sampler._stack_cpu_seconds


def test_unmapped_native_thread_is_attributed_without_a_python_frame() -> None:
    snapshots = iter(
        (
            {777: 5.0},
            {777: 5.4},
        )
    )
    sampler = cpu_sampler.CpuSampler(
        thread_cpu_reader=lambda: next(snapshots),
        threads=lambda: [],
        current_frames=lambda: {},
    )

    assert sampler.sample_once() == 0
    assert sampler.sample_once() == 1
    assert sampler._thread_cpu_seconds["unmapped-native-N"] == pytest.approx(0.4)
    assert sampler._stack_cpu_seconds[
        "thread_group=unmapped-native-N sample=unmapped stack=python-unmapped"
    ] == pytest.approx(0.4)


def test_thread_groups_and_idle_samples_are_stable_and_visible() -> None:
    assert cpu_sampler._thread_group("db_12") == "db_N"
    assert cpu_sampler._thread_group("ThreadPoolExecutor-12_3") == "ThreadPoolExecutor-N_N"

    frame = type(
        "Frame",
        (),
        {
            "f_code": type(
                "Code",
                (),
                {
                    "co_filename": f"{cpu_sampler._STDLIB_PREFIX}concurrent/futures/thread.py",
                    "co_name": "_worker",
                },
            )(),
            "f_lineno": 42,
            "f_back": None,
        },
    )()
    assert cpu_sampler._signature(frame, "ThreadPoolExecutor-N_N") == (
        "thread_group=ThreadPoolExecutor-N_N sample=idle "
        "stack=stdlib/thread.py:_worker:42"
    )


def test_stack_signature_keeps_six_innermost_frames() -> None:
    frame = None
    for line in reversed(range(7)):
        frame = type(
            "Frame",
            (),
            {
                "f_code": type(
                    "Code", (), {"co_filename": "/tmp/stack.py", "co_name": f"frame_{line}"}
                )(),
                "f_lineno": line,
                "f_back": frame,
            },
        )()

    assert cpu_sampler._signature(frame, "worker") == (
        "thread_group=worker sample=active stack=external/stack.py:frame_0:0 <- "
        "external/stack.py:frame_1:1 <- external/stack.py:frame_2:2 <- "
        "external/stack.py:frame_3:3 <- external/stack.py:frame_4:4 <- "
        "external/stack.py:frame_5:5"
    )


def test_summary_building_stays_cheap_with_sixty_thread_groups(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="infra.cpu_sampler")
    sampler = cpu_sampler.CpuSampler()
    sampler._process_cpu_seconds = 6.0
    sampler._thread_cpu_seconds = {f"worker-{index}": 0.1 for index in range(60)}
    sampler._stack_cpu_seconds = {
        (
            f"thread_group=worker-{index} sample=active "
            f"stack=backend/x.py:work:{index} <- backend/y.py:caller:{index}"
        ): 0.1
        for index in range(60)
    }

    started = time.process_time()
    sampler.emit_summary(window_seconds=60)

    assert time.process_time() - started < 0.1
    assert len(caplog.records[-1].getMessage()) <= cpu_sampler.MAX_SUMMARY_CHARS


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc format")
def test_linux_stat_parser_handles_parentheses_and_spaces_in_comm() -> None:
    fields = ["S", *["0"] * 10, "17", "19"]
    stat = f"123 (worker name) with parens) {' '.join(fields)}"

    assert thread_cpu._linux_stat_cpu_ticks(stat) == (17, 19)


def test_darwin_reader_deallocates_every_port_and_array_when_thread_info_fails() -> None:
    class FakeLibc:
        def __init__(self) -> None:
            self.ports = (thread_cpu._MACH_PORT_T * 2)(101, 102)
            self.deallocated_ports: list[int] = []
            self.deallocated_arrays: list[tuple[int, int]] = []

        def task_threads(self, _task, threads_out, count_out) -> int:
            ctypes.cast(
                threads_out,
                ctypes.POINTER(ctypes.POINTER(thread_cpu._MACH_PORT_T)),
            )[0] = ctypes.cast(self.ports, ctypes.POINTER(thread_cpu._MACH_PORT_T))
            ctypes.cast(count_out, ctypes.POINTER(thread_cpu._MACH_MSG_TYPE_NUMBER_T))[0] = 2
            return 0

        def thread_info(self, port, flavor, _out, _count) -> int:
            # The first port cannot identify itself; the second cannot read CPU time.
            return 1 if port == 101 and flavor == thread_cpu._THREAD_IDENTIFIER_INFO else 1

        def mach_port_deallocate(self, _task, port) -> int:
            self.deallocated_ports.append(port)
            return 0

        def vm_deallocate(self, _task, address, size) -> int:
            self.deallocated_arrays.append((int(address.value), int(size)))
            return 0

    libc = FakeLibc()

    assert thread_cpu._darwin_thread_cpu_seconds(libc, 77) == {}
    assert libc.deallocated_ports == [101, 102]
    assert libc.deallocated_arrays == [
        (ctypes.addressof(libc.ports), 2 * ctypes.sizeof(thread_cpu._MACH_PORT_T))
    ]


def test_unavailable_reader_prevents_start_and_logs_once(monkeypatch, caplog) -> None:
    cpu_sampler.stop_cpu_sampler()
    caplog.set_level(logging.WARNING, logger="infra.cpu_sampler")
    monkeypatch.setenv(cpu_sampler.ENABLE_ENV, "1")
    monkeypatch.setattr(cpu_sampler, "_unavailable_logged", False)
    monkeypatch.setattr(
        cpu_sampler,
        "thread_cpu_seconds",
        lambda: (_ for _ in ()).throw(thread_cpu.ThreadCpuUnavailable("not available")),
    )

    assert not cpu_sampler.start_cpu_sampler()
    assert not cpu_sampler.start_cpu_sampler()
    assert caplog.text.count("per-thread CPU unavailable; not starting") == 1
    assert cpu_sampler._sampler is None


def test_environment_disable_never_starts_global_sampler(monkeypatch) -> None:
    cpu_sampler.stop_cpu_sampler()
    monkeypatch.setenv(cpu_sampler.ENABLE_ENV, "0")

    assert not cpu_sampler.start_cpu_sampler()
    assert cpu_sampler._sampler is None


def test_apply_cpu_profiler_starts_and_stops_with_the_flag(monkeypatch) -> None:
    cpu_sampler.stop_cpu_sampler()
    monkeypatch.delenv(cpu_sampler.ENABLE_ENV, raising=False)
    try:
        assert cpu_sampler.apply_cpu_profiler(True)
        assert cpu_sampler._sampler is not None and cpu_sampler._sampler.is_running()
        assert cpu_sampler.apply_cpu_profiler(True)

        assert not cpu_sampler.apply_cpu_profiler(False)
        assert cpu_sampler._sampler is None
        assert not cpu_sampler.apply_cpu_profiler(False)
    finally:
        cpu_sampler.stop_cpu_sampler()


def test_apply_cpu_profiler_keeps_the_env_override_on_when_the_flag_is_off(monkeypatch) -> None:
    cpu_sampler.stop_cpu_sampler()
    try:
        assert cpu_sampler.apply_cpu_profiler(False, {cpu_sampler.ENABLE_ENV: "1"})
        assert cpu_sampler._sampler is not None and cpu_sampler._sampler.is_running()
    finally:
        cpu_sampler.stop_cpu_sampler()


def test_boot_applies_the_saved_flag(monkeypatch) -> None:
    applied: list[bool] = []
    monkeypatch.setattr(app_startup_runtime, "apply_cpu_profiler", applied.append)
    import infra.settings as settings

    monkeypatch.setattr(settings, "get_feature_flags", lambda: {"cpu_profiler": True})
    app_startup_runtime._apply_saved_cpu_profiler()
    monkeypatch.setattr(settings, "get_feature_flags", lambda: {})
    app_startup_runtime._apply_saved_cpu_profiler()

    assert applied == [True, False]


def test_boot_starts_the_sampler_from_the_env_override_with_the_flag_off(monkeypatch) -> None:
    cpu_sampler.stop_cpu_sampler()
    import infra.settings as settings

    monkeypatch.setattr(settings, "get_feature_flags", lambda: {})
    monkeypatch.setenv(cpu_sampler.ENABLE_ENV, "1")
    try:
        app_startup_runtime._apply_saved_cpu_profiler()
        assert cpu_sampler.cpu_sampler_running()
    finally:
        cpu_sampler.stop_cpu_sampler()


def test_sampler_is_off_unless_explicitly_enabled() -> None:
    assert not cpu_sampler.sampler_enabled({})
    assert not cpu_sampler.sampler_enabled({cpu_sampler.ENABLE_ENV: "0"})
    assert not cpu_sampler.sampler_enabled({cpu_sampler.ENABLE_ENV: "true"})
    assert cpu_sampler.sampler_enabled({cpu_sampler.ENABLE_ENV: "1"})


def test_default_environment_never_starts_global_sampler(monkeypatch) -> None:
    cpu_sampler.stop_cpu_sampler()
    monkeypatch.delenv(cpu_sampler.ENABLE_ENV, raising=False)

    assert not cpu_sampler.start_cpu_sampler()
    assert cpu_sampler._sampler is None


def test_runtime_log_shutdown_stops_sampler_before_log_capture(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(app_startup_runtime, "stop_cpu_sampler", lambda: calls.append("sampler"))
    monkeypatch.setattr(app_startup_runtime, "stop_corsia_logs", lambda _: calls.append("logs"))

    app_startup_runtime.stop_runtime_logs(logging.getLogger("test"))

    assert calls == ["sampler", "logs"]
