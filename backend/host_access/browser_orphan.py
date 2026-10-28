"""Recover a saved profile from a headless Chrome orphaned by a previous backend."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import psutil

from host_access import browser_chrome, browser_identity as identity, browser_session_launch

logger = logging.getLogger(__name__)
HEADED_HOLDER = "A CORSIA browser window left from before a restart is still open. Please close that window, then try again."
UNVERIFIED_HOLDER = "CORSIA cannot verify or stop browser process {pid}. Please quit that process, then try again; agents cannot quit it."
_SAFE_STARTUP_MESSAGES = frozenset({
    browser_chrome.CHROME_EXITED, browser_chrome.CHROME_NOT_STARTED,
    browser_chrome.LISTENER_UNVERIFIED, browser_chrome.ENDPOINT_MISMATCH,
    browser_session_launch.STREAM_MISMATCH,
})


class BrowserProfileInUse(RuntimeError):
    """A live or unverified browser still holds the saved profile."""

    def __init__(self, pid: int, *, headed: bool = False) -> None:
        super().__init__(HEADED_HOLDER if headed else UNVERIFIED_HOLDER.format(pid=pid))


def startup_message(exc: BaseException) -> str:
    """Only known internal diagnostics are safe to put in the service log."""
    message = str(exc)
    return message if isinstance(exc, BrowserProfileInUse) or message in _SAFE_STARTUP_MESSAGES else "details withheld"


def _still_ours(process: psutil.Process, birth: float, argument: str) -> bool | None:
    try:
        current = identity.process_for_identity(process.pid)
        argv = current.cmdline()
        return True if (current.is_running() and identity.process_create_time(current) == birth
                        and current.ppid() == 1 and current.uids().real == os.getuid()
                        and argument in argv and "--headless=new" in argv
                        and os.getpgid(current.pid) == current.pid) else None
    except (psutil.Error, OSError, identity.BrowserIdentityError, identity.BrowserPidAbsent):
        return None


def close_orphans(profile: str, owned_pids: frozenset[int]) -> None:
    """Close only verified headless Chrome group leaders on this exact profile, off-loop."""
    from host_access.processes import stop_orphan_group

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
                raise BrowserProfileInUse(pid)
            if "--headless=new" not in argv:
                raise BrowserProfileInUse(pid, headed=True)
            candidates.append((process, identity.process_create_time(process)))
        except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError, identity.BrowserPidAbsent):
            continue
        except (psutil.AccessDenied, PermissionError, identity.BrowserIdentityError):
            if matched:
                raise BrowserProfileInUse(pid) from None
    for process, birth in candidates:
        pid = process.pid
        result = stop_orphan_group(
            pid, lambda: _still_ours(process, birth, argument),
            on_signal=lambda: logger.info("signalled orphaned Chrome pid=%s profile=%s", pid, Path(profile).name),
            ignore_zombie_leader=True,
        )
        if result == "unverified":
            raise BrowserProfileInUse(pid)
