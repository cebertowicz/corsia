"""Durable ownership for one local-model setup flight.

The backend process may restart while oMLX continues its download.  This record
keeps the exact model and oMLX task id under the state root; the adjacent
advisory lock makes a second backend wait rather than issue another download.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import fcntl
import json
import logging
import os
from uuid import uuid4
from pathlib import Path
from typing import IO

from llm.local_model import LocalModel, from_recommendation, to_record


logger = logging.getLogger(__name__)

_RECORD_NAME = "local-setup-operation.json"
_LOCK_NAME = "local-setup-operation.lock"


@dataclass(frozen=True)
class Operation:
    repository: str | None
    download_size_bytes: int
    context_window: int
    kind: str
    task_id: str | None = None
    name: str | None = None
    level: float | None = None
    fp16: bool | None = None
    relative_speed: float | None = None
    onboarding: bool = False
    flight_id: str = field(default_factory=lambda: uuid4().hex)
    result: dict | None = None
    downloaded: bool = False
    notified: bool = False
    resident_bytes: int | None = None
    smarts_level: float | None = None
    tokens_per_second: float | None = None
    smarts: float | None = None
    table_on_ssd: bool = False
    table_bytes: int = 0

    @classmethod
    def for_model(cls, model: LocalModel | None, kind: str, task_id: str | None = None, *, onboarding: bool = False) -> Operation:
        """Create a flight before model selection, or with its selected model."""
        if model is None:
            return cls(None, 0, 0, kind, onboarding=onboarding)
        return cls(
            model.repository, model.download_size_bytes, model.context_window, kind, task_id,
            model.name, model.level, model.fp16, model.relative_speed, onboarding,
            resident_bytes=model.resident_bytes, smarts_level=model.smarts_level,
            tokens_per_second=model.tokens_per_second, smarts=model.smarts,
            table_on_ssd=model.table_on_ssd, table_bytes=model.table_bytes,
        )

    @property
    def in_flight(self) -> bool:
        """True until the flight has a terminal result; a download still running in oMLX stays in flight."""
        return self.result is None or bool(self.result.get("downloading"))

    @property
    def model(self) -> LocalModel | None:
        """The selected model, if the flight reached model selection."""
        if self.repository is None:
            return None
        return LocalModel(
            self.repository, self.download_size_bytes, self.context_window, self.name,
            self.level, self.fp16, self.relative_speed, self.resident_bytes,
            self.smarts_level, self.tokens_per_second, self.smarts, self.table_on_ssd, self.table_bytes,
        )


def _state_root() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT)


def record_path() -> Path:
    return _state_root() / _RECORD_NAME


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
        "model" not in payload or (model is None and payload.get("model") is not None)
        or kind not in {"setup", "settings"}
        or (task_id is not None and not isinstance(task_id, str))
        or (payload.get("result") is not None and not isinstance(payload["result"], dict))
    ):
        logger.warning("discarding an invalid or retired local setup operation record")
        clear()
        return None
    return replace(
        Operation.for_model(model, kind, task_id, onboarding=payload.get("onboarding") is True),
        flight_id=payload.get("flight_id") or (model.repository if model else "local-model"),
        result=payload.get("result"), downloaded=payload.get("downloaded") is True,
        notified=payload.get("notified") is True,
    )


def write(operation: Operation) -> None:
    target = record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with staged.open("w", encoding="utf-8") as handle:
            json.dump({"model": to_record(operation.model) if operation.model else None, "kind": operation.kind,
                       "task_id": operation.task_id, "onboarding": operation.onboarding,
                       "flight_id": operation.flight_id, "result": operation.result,
                       "downloaded": operation.downloaded, "notified": operation.notified}, handle, sort_keys=True)
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
    path = _state_root() / _LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


async def wait_for_lock() -> IO[str]:
    """Wait for the durable operation lock; its owner retains the returned handle."""
    while True:
        handle = try_lock()
        if handle is not None:
            return handle
        await asyncio.sleep(0.25)


def unlock(handle: IO[str] | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
