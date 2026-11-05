"""Recover a saved profile from a headless Chrome orphaned by a previous backend."""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from functools import partial
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


def _record_process_matches(pid: int, birth: float, directory: Path, session: str,
                            profile: str | None) -> bool | None:
    try:
        process = identity.process_for_identity(pid)
        if (not process.is_running() or identity.process_create_time(process) != birth
                or process.uids().real != os.getuid() or os.getpgid(pid) != pid):
            return None
        if profile is not None:
            argv = process.cmdline()
            if not (argv and "chrome" in os.path.basename(argv[0]).lower()
                    and f"--user-data-dir={profile}" in argv
                    and not any(arg.startswith("--type=") for arg in argv)):
                return None
            if "--headless=new" not in argv:
                # A visible window stays for the person to close, as in close_orphans.
                logger.info("left orphaned browser window pid=%s for the person", pid)
                return False
            return True
        executable = process.exe()
        if not executable or "agent-browser" not in os.path.basename(executable):
            return None
        return True if identity.daemon_environment_matches(process, session=session, socket_dir=directory) else None
    except (psutil.Error, OSError, identity.BrowserIdentityError, identity.BrowserPidAbsent):
        return None


def _stop_record(pid: int, birth: float, directory: Path, session: str, profile: str | None) -> None:
    from host_access.processes import stop_orphan_group

    check = partial(_record_process_matches, pid, birth, directory, session, profile)
    result = stop_orphan_group(pid, check, ignore_zombie_leader=True)
    if result == "unverified":
        logger.warning("browser orphan could not be verified pid=%s", pid)


def _stop_daemon(path: Path, directory: Path) -> None:
    session = path.stem
    if identity.SESSION_RE.fullmatch(session) is None:
        return
    pid = None
    try:
        raw, _ = identity.read_regular_sidecar(path, 64)
        pid = identity.parse_pid_sidecar(raw)
        process = identity.process_for_identity(pid)
        birth = identity.process_create_time(process)
        _stop_record(pid, birth, directory, session, None)
    except identity.BrowserPidAbsent:
        return
    except (psutil.Error, OSError, identity.BrowserIdentityError):
        if pid is not None:
            logger.warning("browser orphan could not be verified pid=%s", pid)


def _stop_chrome(path: Path, directory: Path) -> None:
    session = path.stem
    if identity.SESSION_RE.fullmatch(session) is None:
        return
    pid = None
    try:
        raw, _ = identity.read_regular_sidecar(path, 4096)
        record = json.loads(raw)
        pid, birth, profile = record["pid"], record["birth"], record["profile"]
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
                or not isinstance(birth, (int, float)) or not isinstance(profile, str) or not profile):
            return
        _stop_record(pid, birth, directory, session, profile)
    except (ValueError, KeyError, TypeError, psutil.Error, OSError, identity.BrowserIdentityError):
        if isinstance(pid, int):
            logger.warning("browser orphan could not be verified pid=%s", pid)


def close_dead_sessions(own_directory: Path, *, root: Path | None = None) -> None:
    """Close lock-backed sessions of dead owners, without touching profile files."""
    from host_access import browser_owner_lock
    from host_access.browser_ports import SOCKET_ROOT

    deadline = time.monotonic() + 10.0
    try:
        own_directory = own_directory.resolve()
        directories = tuple((root or SOCKET_ROOT).resolve(strict=True).iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        logger.warning("browser orphan sweep could not read the socket root: %s", type(error).__name__)
        return
    for index, directory in enumerate(directories):
        if time.monotonic() >= deadline:
            logger.warning("browser orphan sweep hit its time limit; %d directories not checked",
                           len(directories) - index)
            return
        if directory == own_directory:
            continue
        try:
            metadata = directory.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700):
                continue
            fd = browser_owner_lock.acquire(directory, create=False)
            if fd is None:
                continue
            try:
                for path in directory.glob("*.pid"):
                    if time.monotonic() >= deadline:
                        break
                    _stop_daemon(path, directory)
                for path in directory.glob(f"*{browser_owner_lock.CHROME_SUFFIX}"):
                    if time.monotonic() >= deadline:
                        break
                    _stop_chrome(path, directory)
            finally:
                browser_owner_lock.release(fd)
        except (OSError, identity.BrowserIdentityError) as error:
            logger.warning("browser orphan sweep skipped %s: %s", directory.name, type(error).__name__)
            continue
