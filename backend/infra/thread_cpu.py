"""Portable in-process native-thread CPU accounting for the diagnostic sampler."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from typing import Any


class ThreadCpuUnavailable(RuntimeError):
    """Raised when this process cannot take a native-thread CPU snapshot."""


_MACH_PORT_T = ctypes.c_uint32
_MACH_MSG_TYPE_NUMBER_T = ctypes.c_uint32
_KERN_RETURN_T = ctypes.c_int
_VM_ADDRESS_T = ctypes.c_uint64
_THREAD_BASIC_INFO = 3
_THREAD_IDENTIFIER_INFO = 4
_KERN_SUCCESS = 0


class _TimeValue(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]


class _ThreadBasicInfo(ctypes.Structure):
    _fields_ = [
        ("user_time", _TimeValue),
        ("system_time", _TimeValue),
        ("cpu_usage", ctypes.c_int),
        ("policy", ctypes.c_int),
        ("run_state", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("suspend_count", ctypes.c_int),
        ("sleep_time", ctypes.c_int),
    ]


class _ThreadIdentifierInfo(ctypes.Structure):
    _fields_ = [
        ("thread_id", ctypes.c_uint64),
        ("thread_handle", ctypes.c_uint64),
        ("dispatch_qaddr", ctypes.c_uint64),
    ]


def _seconds(value: _TimeValue) -> float:
    return value.seconds + value.microseconds / 1_000_000


def _configure_darwin_libc(libc: Any) -> None:
    """Declare the subset of Mach ABI used here, including 64-bit addresses."""
    thread_array = ctypes.POINTER(_MACH_PORT_T)
    libc.task_threads.argtypes = [
        _MACH_PORT_T,
        ctypes.POINTER(thread_array),
        ctypes.POINTER(_MACH_MSG_TYPE_NUMBER_T),
    ]
    libc.task_threads.restype = _KERN_RETURN_T
    libc.thread_info.argtypes = [
        _MACH_PORT_T,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(_MACH_MSG_TYPE_NUMBER_T),
    ]
    libc.thread_info.restype = _KERN_RETURN_T
    libc.mach_port_deallocate.argtypes = [_MACH_PORT_T, _MACH_PORT_T]
    libc.mach_port_deallocate.restype = _KERN_RETURN_T
    libc.vm_deallocate.argtypes = [_MACH_PORT_T, _VM_ADDRESS_T, ctypes.c_size_t]
    libc.vm_deallocate.restype = _KERN_RETURN_T


def _darwin_libc_and_task() -> tuple[Any, int]:
    library = ctypes.util.find_library("c")
    if library is None:
        raise ThreadCpuUnavailable("Darwin libc is unavailable")
    libc = ctypes.CDLL(library)
    _configure_darwin_libc(libc)
    try:
        task = _MACH_PORT_T.in_dll(libc, "mach_task_self_").value
    except ValueError as error:
        raise ThreadCpuUnavailable("mach_task_self_ is unavailable") from error
    return libc, task


def _darwin_thread_cpu_seconds(libc: Any, task: int) -> dict[int, float]:
    threads = ctypes.POINTER(_MACH_PORT_T)()
    count = _MACH_MSG_TYPE_NUMBER_T()
    result = libc.task_threads(task, ctypes.byref(threads), ctypes.byref(count))
    if result != _KERN_SUCCESS:
        raise ThreadCpuUnavailable(f"task_threads failed: {result}")

    values: dict[int, float] = {}
    try:
        for index in range(count.value):
            port = threads[index]
            try:
                identifier = _ThreadIdentifierInfo()
                identifier_count = _MACH_MSG_TYPE_NUMBER_T(ctypes.sizeof(identifier) // 4)
                if (
                    libc.thread_info(
                        port,
                        _THREAD_IDENTIFIER_INFO,
                        ctypes.byref(identifier),
                        ctypes.byref(identifier_count),
                    )
                    != _KERN_SUCCESS
                ):
                    continue

                basic = _ThreadBasicInfo()
                basic_count = _MACH_MSG_TYPE_NUMBER_T(ctypes.sizeof(basic) // 4)
                if (
                    libc.thread_info(
                        port,
                        _THREAD_BASIC_INFO,
                        ctypes.byref(basic),
                        ctypes.byref(basic_count),
                    )
                    != _KERN_SUCCESS
                ):
                    continue
                values[identifier.thread_id] = _seconds(basic.user_time) + _seconds(basic.system_time)
            finally:
                libc.mach_port_deallocate(task, port)
    finally:
        address = ctypes.cast(threads, ctypes.c_void_p).value or 0
        libc.vm_deallocate(
            task,
            _VM_ADDRESS_T(address),
            ctypes.sizeof(_MACH_PORT_T) * count.value,
        )
    return values


def _linux_stat_cpu_ticks(stat: str) -> tuple[int, int]:
    """Return utime and stime from /proc stat after its parenthesized comm field."""
    closing_paren = stat.rfind(")")
    if closing_paren < 0:
        raise ValueError("missing comm terminator")
    fields = stat[closing_paren + 1 :].split()
    # The post-comm fields begin at process field 3 (state), so 14/15 are 11/12.
    if len(fields) <= 12:
        raise ValueError("truncated stat fields")
    return int(fields[11]), int(fields[12])


def _linux_thread_cpu_seconds(task_root: str = "/proc/self/task") -> dict[int, float]:
    try:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
    except (AttributeError, OSError, ValueError) as error:
        raise ThreadCpuUnavailable("SC_CLK_TCK is unavailable") from error
    if not isinstance(ticks_per_second, int) or ticks_per_second <= 0:
        raise ThreadCpuUnavailable("invalid SC_CLK_TCK")

    values: dict[int, float] = {}
    try:
        task_entries = os.scandir(task_root)
    except OSError as error:
        raise ThreadCpuUnavailable(f"cannot read {task_root}: {error}") from error
    with task_entries:
        for entry in task_entries:
            try:
                native_id = int(entry.name)
            except ValueError:
                continue
            try:
                with open(os.path.join(task_root, entry.name, "stat"), encoding="utf-8") as stat_file:
                    user_ticks, system_ticks = _linux_stat_cpu_ticks(stat_file.read())
            except FileNotFoundError:
                # Threads can exit between listing the task directory and reading stat.
                continue
            except (OSError, ValueError) as error:
                raise ThreadCpuUnavailable(f"cannot read thread {native_id} CPU: {error}") from error
            values[native_id] = (user_ticks + system_ticks) / ticks_per_second
    return values


def thread_cpu_seconds() -> dict[int, float]:
    """Return native-thread CPU seconds keyed by Python ``Thread.native_id``."""
    if sys.platform == "darwin":
        libc, task = _darwin_libc_and_task()
        return _darwin_thread_cpu_seconds(libc, task)
    if sys.platform.startswith("linux"):
        return _linux_thread_cpu_seconds()
    raise ThreadCpuUnavailable(f"unsupported platform: {sys.platform}")
