"""Durable Settings-owned local-model download state.

Setup keeps one separate marker for the model it installed.  This module records
only models a person added later from Settings, so neither flow can adopt the
other's completion, recovery, or fallback semantics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from llm.local_model import LocalModel, from_recommendation, to_record

_RECORD_NAME = "local-downloads.json"


@dataclass(frozen=True)
class Download:
    model: LocalModel
    status: str = "available"
    speed: float | None = None
    detail: str | None = None

    def view(self) -> dict:
        return {**self.model.view(), "status": self.status, "speed": self.speed, "detail": self.detail}


def _path() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / _RECORD_NAME


def _read_raw() -> dict[str, object]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def all() -> list[Download]:
    result: list[Download] = []
    for item in _read_raw().values():
        if not isinstance(item, dict):
            continue
        model = from_recommendation(item.get("model"))
        status = item.get("status")
        speed = item.get("speed")
        detail = item.get("detail")
        if model is None or status not in {"available", "failed"}:
            continue
        result.append(Download(
            model, status, speed if isinstance(speed, (int, float)) else None,
            detail if isinstance(detail, str) else None,
        ))
    return sorted(result, key=lambda entry: entry.model.repository)


def get(repository: str) -> Download | None:
    return next((entry for entry in all() if entry.model.repository == repository), None)


def write(download: Download) -> None:
    values = _read_raw()
    values[download.model.repository] = {
        "model": to_record(download.model), "status": download.status,
        "speed": download.speed, "detail": download.detail,
    }
    target = _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        staged.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def remove(repository: str) -> None:
    values = _read_raw()
    if repository not in values:
        return
    del values[repository]
    target = _path()
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        staged.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
