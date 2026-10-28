"""Recover a saved profile from a Chrome orphaned by a previous backend."""
from __future__ import annotations

import os
import signal
import time

import psutil

from host_access import browser_identity as identity


class BrowserProfileInUse(RuntimeError):
    """A live or unverified browser still holds the saved profile."""

    def __init__(self) -> None:
        super().__init__("Another browser is using the saved sign-in. Close it and try again.")


_SAFE_STARTUP_MESSAGES = frozenset({
    "Another browser is using the saved sign-in. Close it and try again.",
    "managed Chrome exited during startup",
    "managed Chrome did not start its reserved listener",
    "managed browser listener ownership could not be verified",
    "managed browser endpoint did not match its reserved listener",
    "managed browser stream did not use its reserved listener",
    "orphaned managed Chrome process group did not exit",
})


def startup_message(exc: BaseException) -> str:
    """Only known internal diagnostics are safe to put in the service log."""
    message = str(exc)
    return message if message in _SAFE_STARTUP_MESSAGES else "details withheld"


def close_orphans(profile: str, owned_pids: frozenset[int]) -> None:
    """Close only orphaned Chrome group leaders using this exact profile, off-loop.

    A managed Chrome is a session and group leader. Requiring reparenting to
    init avoids disturbing Chrome owned by another still-running backend.
    """
    argument = f"--user-data-dir={profile}"
    candidates = []
    for process in psutil.process_iter(["pid"]):
        matched = False
        try:
            pid = process.pid
            argv = process.cmdline()
            if (pid <= 1 or not argv or argument not in argv
                    or "chrome" not in os.path.basename(argv[0]).lower()
                    or any(arg.startswith("--type=") for arg in argv)):
                continue
            matched = True
            if (pid in owned_pids or process.ppid() != 1 or process.uids().real != os.getuid()
                    or os.getpgid(pid) != pid):
                raise BrowserProfileInUse()
            candidates.append((process, identity.process_create_time(process)))
        except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError, identity.BrowserPidAbsent):
            continue
        except (psutil.AccessDenied, PermissionError):
            if matched:
                raise BrowserProfileInUse() from None
    for process, birth in candidates:
        try:
            pid = process.pid
            if (not process.is_running() or identity.process_create_time(process) != birth
                    or process.ppid() != 1 or argument not in process.cmdline()
                    or os.getpgid(pid) != pid):
                raise BrowserProfileInUse()
            try:
                _terminate_group(pid)
            except RuntimeError as exc:
                raise BrowserProfileInUse() from exc
        except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError, identity.BrowserPidAbsent):
            continue
        except (psutil.AccessDenied, PermissionError):
            raise BrowserProfileInUse() from None


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_for_group(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _group_exists(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _terminate_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if _wait_for_group(pid, 5):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not _wait_for_group(pid, 5):
        raise RuntimeError("orphaned managed Chrome process group did not exit")
