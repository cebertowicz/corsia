"""Durable ownership for one local-model setup flight.

The backend process may restart while oMLX continues its download.  This record
keeps the exact profile and oMLX task id under the state root; the adjacent
advisory lock makes a second backend wait rather than issue another download.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
from typing import IO


_RECORD_NAME = "local-setup-operation.json"
_LOCK_NAME = "local-setup-operation.lock"


@dataclass(frozen=True)
class Operation:
    profile_id: str
    kind: str
    task_id: str | None = None


def _state_root() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT)


def record_path() -> Path:
    return _state_root() / _RECORD_NAME


def lock_path() -> Path:
    return _state_root() / _LOCK_NAME


def read() -> Operation | None:
    try:
        payload = json.loads(record_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    profile_id, kind, task_id = payload.get("profile_id"), payload.get("kind"), payload.get("task_id")
    if not isinstance(profile_id, str) or not isinstance(kind, str):
        return None
    if task_id is not None and not isinstance(task_id, str):
        return None
    return Operation(profile_id=profile_id, kind=kind, task_id=task_id)


def write(operation: Operation) -> None:
    target = record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with staged.open("w", encoding="utf-8") as handle:
            json.dump(asdict(operation), handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def clear() -> None:
    try:
        record_path().unlink()
    except FileNotFoundError:
        pass


def try_lock() -> IO[str] | None:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def unlock(handle: IO[str] | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
