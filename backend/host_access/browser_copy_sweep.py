"""Remove old, unowned temporary browser profiles at backend startup."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import stat
from pathlib import Path

from host_access import browser_identity, browser_launch

log = logging.getLogger(__name__)


def lock_profile_root(root: Path, *, exclusive: bool, nonblocking: bool = False) -> int:
    """Coordinate copy creation and sweep across backend processes."""
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("invalid browser copy lock")
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (fcntl.LOCK_NB if nonblocking else 0))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _live_profiles(socket_root: Path) -> set[str]:
    try:
        directories = list(socket_root.iterdir())
    except FileNotFoundError:
        return set()
    live: set[str] = set()
    for directory in directories:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            continue
        for sidecar in directory.glob("*.chrome"):
            raw, _ = browser_identity.read_regular_sidecar(sidecar, 4096)
            data = json.loads(raw)
            pid, profile = data["pid"], data["profile"]
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or not isinstance(profile, str):
                raise ValueError("invalid browser owner record")
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass  # An uninspectable group is treated as live.
            live.add(profile)
    return live


def sweep(*, profile_root: Path | None = None, socket_root: Path | None = None) -> int:
    """Remove only old directories absent from all live browser process records."""
    from host_access.browser_ports import SOCKET_ROOT
    root = profile_root or browser_launch.profile_root()
    try:
        if not stat.S_ISDIR(root.lstat().st_mode) or not shutil.rmtree.avoids_symlink_attacks:
            return 0
        fd = lock_profile_root(root, exclusive=True, nonblocking=True)
    except BlockingIOError:
        log.info("temporary browser profiles removed: %d", 0)
        return 0
    except (OSError, ValueError):
        log.warning("temporary browser profile sweep could not verify ownership")
        return 0
    try:
        try:
            live = _live_profiles(socket_root or SOCKET_ROOT)
            candidates = list(root.glob("session-*"))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, browser_identity.BrowserIdentityError):
            log.warning("temporary browser profile sweep could not verify ownership")
            return 0
        removed = 0
        for folder in candidates:
            try:
                info = folder.lstat()
                if not stat.S_ISDIR(info.st_mode) or str(folder) in live:
                    continue
                shutil.rmtree(folder)
                removed += 1
            except OSError:
                continue
        log.info("temporary browser profiles removed: %d", removed)
        return removed
    finally:
        os.close(fd)
