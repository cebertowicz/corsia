"""Durable Settings-owned local-model download state.

Setup keeps one separate marker for the model it installed.  This module records
only models a person added later from Settings, so neither flow can adopt the
other's completion, recovery, or fallback semantics.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from llm.local_model import LocalModel, from_recommendation, to_record

_RECORD_NAME = "local-downloads.json"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Download:
    model: LocalModel
    status: str = "available"
    speed: float | None = None
    detail: str | None = None
    # The local server that completed the download; absent for legacy records.
    server_id: str | None = None

    def view(self) -> dict:
        return {
            **self.model.view(), "status": self.status, "speed": self.speed,
            "detail": self.detail, "server_id": self.server_id,
        }


def _path() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / _RECORD_NAME


def _read_raw() -> dict[str, object]:
    target = _path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Couldn't read Settings local-download state: %s", exc)
        corrupt = target.with_name(f"{target.name}.corrupt")
        try:
            os.replace(target, corrupt)
        except OSError as move_exc:
            log.warning("Couldn't preserve corrupt Settings local-download state: %s", move_exc)
            raise
        return {}
    if not isinstance(raw, dict):
        log.warning("Couldn't read Settings local-download state: expected an object")
        os.replace(target, target.with_name(f"{target.name}.corrupt"))
        return {}
    return raw


def all() -> list[Download]:
    result: list[Download] = []
    for item in _read_raw().values():
        if not isinstance(item, dict):
            continue
        model = from_recommendation(item.get("model"))
        status = item.get("status")
        speed = item.get("speed")
        detail = item.get("detail")
        server_id = item.get("server_id")
        if model is None or status not in {"available", "failed"}:
            continue
        result.append(Download(
            model, status, speed if isinstance(speed, (int, float)) else None,
            detail if isinstance(detail, str) else None,
            server_id if isinstance(server_id, str) else None,
        ))
    return sorted(result, key=lambda entry: entry.model.repository)


def get(model_id: str) -> Download | None:
    """Return a Settings record by oMLX model id (legacy repository callers still read)."""
    return next((entry for entry in all() if entry.model.model == model_id or entry.model.repository == model_id), None)


def forget(model_id: str) -> None:
    """Remove the one Settings record owned by this oMLX model id."""
    values = _read_raw()
    keys = [key for key, item in values.items()
            if key == model_id or (isinstance(item, dict)
            and (model := from_recommendation(item.get("model"))) is not None and model.model == model_id)]
    if not keys:
        return
    for key in keys:
        del values[key]
    _write_raw(values)


def remove(model_id: str) -> None:
    """Compatibility alias for callers still naming a Settings record by repository."""
    record = get(model_id)
    if record is not None:
        forget(record.model.model)


def write(download: Download) -> None:
    values = _read_raw()
    # Migrate a legacy repository key when this model is written again.
    for key, item in list(values.items()):
        if isinstance(item, dict) and (model := from_recommendation(item.get("model"))) is not None and model.model == download.model.model:
            del values[key]
    values[download.model.model] = {
        "model": to_record(download.model), "status": download.status,
        "speed": download.speed, "detail": download.detail,
        "server_id": download.server_id,
    }
    _write_raw(values)


def _write_raw(values: dict[str, object]) -> None:
    target = _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        staged.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
