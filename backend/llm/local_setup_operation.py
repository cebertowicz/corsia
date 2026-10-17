"""Durable ownership for one local-model setup flight.

The backend process may restart while oMLX continues its download.  This record
keeps the exact model and oMLX task id under the state root; the adjacent
advisory lock makes a second backend wait rather than issue another download.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import IO

from llm.local_model import LocalModel, from_recommendation, to_record


logger = logging.getLogger(__name__)

_RECORD_NAME = "local-setup-operation.json"
_LOCK_NAME = "local-setup-operation.lock"


@dataclass(frozen=True)
class Operation:
    repository: str
    download_size_bytes: int
    context_window: int
    kind: str
    task_id: str | None = None
    name: str | None = None

    @classmethod
    def for_model(cls, model: LocalModel, kind: str, task_id: str | None = None) -> Operation:
        return cls(model.repository, model.download_size_bytes, model.context_window, kind, task_id, model.name)

    @property
    def model(self) -> LocalModel:
        return LocalModel(self.repository, self.download_size_bytes, self.context_window, self.name)


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
    except (OSError, ValueError) as error:
        logger.warning("local setup operation record is unreadable; treating it as absent: %s", error)
        return None
    if not isinstance(payload, dict):
        payload = {}
    model = from_recommendation(payload.get("model"))
    kind = payload.get("kind")
    task_id = payload.get("task_id")
    # A record from before it kept the recommender row (a profile id, or flat model
    # fields): malformed here, so it is discarded and the next request starts afresh.
    if (
        model is None
        or not isinstance(kind, str)
        or (task_id is not None and not isinstance(task_id, str))
    ):
        logger.warning("local setup operation record is malformed; treating it as absent")
        return None
    return Operation.for_model(model, kind, task_id)


def write(operation: Operation) -> None:
    target = record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with staged.open("w", encoding="utf-8") as handle:
            json.dump({"model": to_record(operation.model), "kind": operation.kind,
                       "task_id": operation.task_id}, handle, sort_keys=True)
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
