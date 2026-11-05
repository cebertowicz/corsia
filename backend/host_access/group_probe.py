"""Process-group liveness probing."""

from __future__ import annotations

import os

import psutil


def current_control_plane_pids() -> frozenset[int]:
    protected = {0, 1, os.getpid()}
    current = os.getpid()
    seen: set[int] = set()
    while current > 1 and current not in seen:
        seen.add(current)
        protected.add(current)
        try:
            current = psutil.Process(current).ppid()
        except psutil.Error:
            break
    return frozenset(protected)


def is_zombie(process: psutil.Process) -> bool:
    try:
        return process.status() in {
            psutil.STATUS_ZOMBIE,
            getattr(psutil, "STATUS_DEAD", "dead"),
            "terminated",
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


def group_pids(pgid: int, *, ignore_zombie_leader: bool = False) -> tuple[int, ...]:
    if pgid <= 1:
        return ()
    # Exact process-group existence does not require broad host enumeration.
    # This is both a tighter ownership proof and works in the protected
    # envelope where macOS denies the sysctl used by ``psutil.pids()``.
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return ()
    except PermissionError:
        return live_group_members_after_eperm(pgid, ignore_zombie_leader=ignore_zombie_leader)
    return (pgid,)


def live_group_members_after_eperm(pgid: int, *, ignore_zombie_leader: bool = False) -> tuple[int, ...]:
    """Resolve an EPERM group probe by looking for a non-zombie member.

    macOS answers EPERM, not success, for ``killpg(pgid, 0)`` when every
    remaining member is an unreaped zombie, so EPERM alone does not prove a
    live member. An unreaped zombie leader still counts as present: its owner
    reaps it, and the stop path settles the group only after that reap. For
    orphan recovery there is no child handle to reap, so its caller can instead
    check for any live member while ignoring the unreaped zombie leader.

    Any member whose state cannot be read, or an enumeration the envelope
    denies, keeps the conservative answer that the group still exists.
    """
    try:
        if not ignore_zombie_leader and psutil.Process(pgid).status() == psutil.STATUS_ZOMBIE:
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
