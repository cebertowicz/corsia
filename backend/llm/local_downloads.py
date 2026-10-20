"""Durable Settings-owned local-model download state.

Setup keeps one separate marker for the model it installed.  This module records
only models a person added later from Settings, so neither flow can adopt the
other's completion, recovery, or fallback semantics.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
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
    # On a failed record: the bytes are on disk and only listing the model failed.
    downloaded: bool = False

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
        return _set_aside(target, str(exc))
    if not isinstance(raw, dict):
        return _set_aside(target, "expected an object")
    return raw


def _set_aside(target: Path, reason: str) -> dict[str, object]:
    """Keep an unreadable record as ``.corrupt`` so the next write cannot overwrite it."""
    log.warning("Couldn't read Settings local-download state: %s", reason)
    try:
        os.replace(target, target.with_name(f"{target.name}.corrupt"))
    except OSError as exc:
        log.warning("Couldn't preserve corrupt Settings local-download state: %s", exc)
        raise
    return {}


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
            item.get("downloaded") is True,
        ))
    return sorted(result, key=lambda entry: entry.model.repository)


def get(model_id: str) -> Download | None:
    """Return the Settings record keyed by this oMLX model id."""
    return next((entry for entry in all() if entry.model.model == model_id), None)


def forget(model_id: str) -> None:
    """Remove the Settings record keyed by this oMLX model id."""
    values = _read_raw()
    if values.pop(model_id, None) is not None:
        _write_raw(values)


def set_detail(model_id: str, detail: str | None) -> None:
    """Say why a downloaded model is not loaded (None clears it); a failed or absent record is left alone."""
    record = get(model_id)
    if record is not None and record.status == "available" and record.detail != detail:
        write(replace(record, detail=detail))


def reconcile(on_disk: set[str]) -> None:
    """Forget completed Settings downloads that oMLX no longer has on disk."""
    for download in all():
        if download.status == "available" and download.model.model not in on_disk:
            forget(download.model.model)


def write(download: Download) -> None:
    values = _read_raw()
    values[download.model.model] = {
        "model": to_record(download.model), "status": download.status,
        "speed": download.speed, "detail": download.detail,
        "server_id": download.server_id, "downloaded": download.downloaded,
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
