"""Whether CORSIA manages the person's own oMLX, and the values it replaced there.

"Let CORSIA manage it" (setup's Welcome, or Settings) is the person's consent
to read their oMLX's admin key and change the settings CORSIA shows them first:
the two oMLX-wide settings, and the settings of the one model CORSIA downloaded.
This record is the one owner of that state. It keeps each value as it was
before CORSIA's first change to it (a later change never replaces it), written
before any change is sent, so stopping management can put every one back. A
restore that fails keeps the record, so it can be tried again.

The record never holds a key: the key stays in oMLX's own settings file and,
once read, in the key store under the server.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from llm.local_model import LocalModel, from_recommendation

log = logging.getLogger(__name__)

_RECORD_NAME = "own-omlx-management.json"


@dataclass(frozen=True)
class Management:
    """``active`` is false once stopping began; ``originals`` then waits to be restored."""

    active: bool = True
    #: The model CORSIA downloaded into their oMLX, the only one whose settings it writes.
    model: LocalModel | None = None
    originals_global: dict[str, Any] = field(default_factory=dict)
    originals_model: dict[str, Any] = field(default_factory=dict)
    #: Whether CORSIA stored the admin key itself, so stopping removes it.
    key_registered: bool = False
    #: Their oMLX's version when it is too old to ask which model fits, else None.
    cannot_check: str | None = None
    restore_failed: bool = False


def record_path() -> Path:
    """The record's file in this CORSIA state root."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / _RECORD_NAME


def read() -> Management | None:
    """The record, or None when CORSIA never managed this oMLX (or the record is unreadable)."""
    try:
        raw = json.loads(record_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        log.warning("own oMLX management record is unreadable; treating it as absent: %s", error)
        return None
    if not isinstance(raw, dict):
        log.warning("own oMLX management record is malformed; treating it as absent")
        return None
    model = raw.get("model")
    globals_, models = raw.get("originals_global"), raw.get("originals_model")
    cannot_check = raw.get("cannot_check")
    return Management(
        active=raw.get("active") is True,
        model=from_recommendation({
            "repo_id": model.get("repository"), "download_bytes": model.get("download_size_bytes"),
            "max_context": model.get("context_window"),
        }) if isinstance(model, dict) else None,
        originals_global=globals_ if isinstance(globals_, dict) else {},
        originals_model=models if isinstance(models, dict) else {},
        key_registered=raw.get("key_registered") is True,
        cannot_check=cannot_check if isinstance(cannot_check, str) else None,
        restore_failed=raw.get("restore_failed") is True,
    )


def _write(record: Management) -> None:
    target = record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**asdict(record), "model": asdict(record.model) if record.model else None}
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        staged.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def active() -> bool:
    """Whether CORSIA manages the person's oMLX now: stopping, or never managing, reads False."""
    record = read()
    return record is not None and record.active


def consent() -> None:
    """The person chose to let CORSIA manage their oMLX; values recorded earlier are kept."""
    record = read() or Management()
    _write(replace(record, active=True, restore_failed=False))


def update(**changes: Any) -> None:
    """Change fields of an active record (the flight's model, key and version findings)."""
    record = read()
    if record is not None and record.active:
        _write(replace(record, **changes))


def record_originals(global_values: dict[str, Any], model_values: dict[str, Any]) -> None:
    """Keep each value as it stood before CORSIA's first change to it, before that change is sent.

    Raises when CORSIA isn't managing this oMLX or the record cannot be written;
    the caller then sends nothing.
    """
    record = read()
    if record is None or not record.active:
        raise RuntimeError("CORSIA isn't managing this oMLX")
    _write(replace(
        record,
        originals_global={**global_values, **record.originals_global},
        originals_model={**model_values, **record.originals_model},
    ))


def begin_stop() -> Management | None:
    """Stop managing now: no further change is offered or sent. Returns what to restore."""
    record = read()
    if record is None:
        return None
    stopped = replace(record, active=False)
    _write(stopped)
    return stopped


def restore_failed() -> None:
    """A restore didn't finish: keep the record, marked, so Settings offers Try again."""
    record = read()
    if record is not None:
        _write(replace(record, restore_failed=True))


def finish_stop() -> None:
    """Every value is back: forget the record."""
    record_path().unlink(missing_ok=True)
