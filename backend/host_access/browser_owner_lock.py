"""The browser owner's lifetime lock and its direct Chrome process records."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path

from host_access import browser_identity as identity

LOCK_NAME = "owner.lock"
CHROME_SUFFIX = ".chrome"


def acquire(directory: Path, *, create: bool) -> int | None:
    """Hold a private owner lock or return None when it is absent or already held."""
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(directory / LOCK_NAME, flags, 0o600)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise identity.BrowserIdentityError("browser owner lock is invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except BaseException:
        os.close(fd)
        raise


def release(fd: int) -> None:
    """Release the lifetime lock after the owner's sessions have closed."""
    os.close(fd)


def record_launched_chrome(directory: Path, session: str, chrome, profile: str | None) -> None:
    """Record a just-launched Chrome before the native daemon attaches."""
    pid = chrome.process.pid
    process = identity.process_for_identity(pid)
    birth = identity.process_create_time(process)
    target = profile or str(chrome.temporary_profile)
    record_chrome(directory, session, pid, birth, target)


def record_chrome(directory: Path, session: str, pid: int, birth: float, profile: str) -> None:
    """Atomically record a Chrome group alongside its session's native sidecars."""
    if identity.SESSION_RE.fullmatch(session) is None or pid <= 1:
        raise identity.BrowserIdentityError("browser Chrome identity is invalid")
    path = directory / f"{session}{CHROME_SUFFIX}"
    fd, temporary = tempfile.mkstemp(prefix=".chrome-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": pid, "birth": birth, "profile": profile}, stream)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
