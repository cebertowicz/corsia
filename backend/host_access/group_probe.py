"""Process-group liveness probing."""

from __future__ import annotations

import os

import psutil


def live_group_members_after_eperm(pgid: int) -> tuple[int, ...]:
    """Resolve an EPERM group probe by looking for a non-zombie member.

    macOS answers EPERM, not success, for ``killpg(pgid, 0)`` when every
    remaining member is an unreaped zombie, so EPERM alone does not prove a
    live member. An unreaped zombie leader still counts as present: its owner
    reaps it, and the stop path settles the group only after that reap.

    Any member whose state cannot be read, or an enumeration the envelope
    denies, keeps the conservative answer that the group still exists.
    """
    try:
        if psutil.Process(pgid).status() == psutil.STATUS_ZOMBIE:
            return (pgid,)
    except psutil.NoSuchProcess:
        pass
    except (psutil.Error, OSError, SystemError):
        return (pgid,)
    try:
        candidates = tuple(psutil.process_iter(["pid"]))
    except (psutil.Error, OSError, SystemError):
        return (pgid,)
    for candidate in candidates:
        try:
            if os.getpgid(candidate.pid) != pgid:
                continue
            if candidate.status() not in {
                psutil.STATUS_ZOMBIE,
                getattr(psutil, "STATUS_DEAD", "dead"),
                "terminated",
            }:
                return (pgid,)
        except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (PermissionError, psutil.AccessDenied, OSError, SystemError):
            return (pgid,)
    return ()
