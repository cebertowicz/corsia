"""Whether CORSIA manages the person's own oMLX, and the values it replaced there.

"Let CORSIA manage it" (setup's Welcome, or Settings) is the person's consent
to read their oMLX's admin key and change the settings CORSIA shows them first:
the two oMLX-wide settings, and the settings of the one model CORSIA downloaded.
This record is the one owner of that state. It keeps each value as it was
before CORSIA's first change to it (a later change never replaces it), written
before any change is sent, so stopping management can put every one back. A
restore that fails keeps what is still to put back, so it can be tried again.
The values are kept per model: the model CORSIA sets up can change (a later
pick), and each model gets back only its own values.

"Use mine" is the person's other choice: CORSIA reads no key and changes
nothing, and offers no setting to change. It is kept apart from setup's consent
to look at their oMLX, so an install that never saw these two choices keeps
its earlier behavior. Stopping management is choosing Use mine.

The record never holds a key: the key stays in oMLX's own settings file and,
once read, in the key store under the server.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from llm.local_model import LocalModel, from_recommendation, to_record

log = logging.getLogger(__name__)

_RECORD_NAME = "own-omlx-management.json"
_USE_MINE_NAME = "own-omlx-use-mine"
#: Mutators run on worker threads (``asyncio.to_thread``); each read-modify-write holds this.
_LOCK = threading.RLock()


@dataclass(frozen=True)
class Management:
    """``active`` is false once stopping began; ``originals`` then waits to be restored."""

    active: bool = True
    #: The model CORSIA downloaded into their oMLX, the only one whose settings it writes.
    model: LocalModel | None = None
    #: ``model`` is the starter because their oMLX's recommender had no pick (setup says so).
    fallback: bool = False
    originals_global: dict[str, Any] = field(default_factory=dict)
    #: oMLX model id -> that model's values before CORSIA's first change to each.
    originals_models: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Whether CORSIA stored the admin key itself, so stopping removes it.
    key_registered: bool = False
    #: Their oMLX's version when it is too old to ask which model fits, else None.
    cannot_check: str | None = None
    restore_failed: bool = False
    #: Whether the person chose "Let CORSIA manage it". A consented legacy flow's restore
    #: record is active without it, and never counts as consent to manage.
    consented: bool = False

    @property
    def manages(self) -> bool:
        """Whether CORSIA manages their oMLX now: chosen by the person, and not stopping."""
        return self.active and self.consented


def record_path() -> Path:
    """The record's file in this CORSIA state root."""
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / _RECORD_NAME


def _use_mine_path() -> Path:
    return record_path().with_name(_USE_MINE_NAME)


def _originals_models(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-model values in the only record shape CORSIA has shipped."""
    models = raw.get("originals_models")
    return {key: dict(values) for key, values in models.items()
            if isinstance(key, str) and isinstance(values, dict)} if isinstance(models, dict) else {}


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
    model = from_recommendation(raw.get("model"))
    globals_ = raw.get("originals_global")
    cannot_check = raw.get("cannot_check")
    return Management(
        active=raw.get("active") is True,
        model=model,
        fallback=raw.get("fallback") is True,
        originals_global=globals_ if isinstance(globals_, dict) else {},
        originals_models=_originals_models(raw),
        key_registered=raw.get("key_registered") is True,
        cannot_check=cannot_check if isinstance(cannot_check, str) else None,
        restore_failed=raw.get("restore_failed") is True,
        consented=raw.get("consented") is True,
    )


def _write(record: Management) -> None:
    target = record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**asdict(record), "model": to_record(record.model) if record.model else None}
    staged = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        staged.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def active() -> bool:
    """Whether CORSIA manages the person's oMLX now: stopping, or never managing, reads False."""
    record = read()
    return record is not None and record.manages


def uses_mine() -> bool:
    """Whether the person chose Use mine (or stopped management): CORSIA offers and sends no change."""
    return _use_mine_path().exists() and not active()


def choose_use_mine() -> None:
    """The person chose Use mine for their oMLX."""
    path = _use_mine_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("use mine\n", encoding="utf-8")


def consent() -> None:
    """The person chose to let CORSIA manage their oMLX; values recorded earlier are kept."""
    with _LOCK:
        record = read() or Management()
        _write(replace(record, active=True, consented=True, restore_failed=False))
        _use_mine_path().unlink(missing_ok=True)


def update(**changes: Any) -> None:
    """Change fields of an active record (the flight's model, key and version findings)."""
    with _LOCK:
        record = read()
        if record is not None and record.manages:
            _write(replace(record, **changes))


def record_originals(global_values: dict[str, Any], model_values: dict[str, dict[str, Any]]) -> None:
    """Keep each value as it stood before CORSIA's first change to it, before that change is sent.

    ``model_values`` maps an oMLX model id to that model's values. A consented
    legacy flow can write before choosing management, so its first write creates
    the active restore record, which is not consent to manage (``consented``
    stays as it was). The caller sends nothing when recording fails.
    """
    with _LOCK:
        record = read() or Management()
        if not record.active:
            record = replace(record, active=True)
        models = {key: dict(values) for key, values in record.originals_models.items()}
        for model_id, values in model_values.items():
            models[model_id] = {**values, **models.get(model_id, {})}
        _write(replace(
            record,
            originals_global={**global_values, **record.originals_global},
            originals_models=models,
        ))


def forget_model_originals(model_id: str) -> None:
    """Remove restored-model bookkeeping after its Settings-owned download is deleted."""
    with _LOCK:
        record = read()
        if record is None or model_id not in record.originals_models:
            return
        originals = {key: value for key, value in record.originals_models.items() if key != model_id}
        _write(replace(record, originals_models=originals))


def begin_stop() -> Management | None:
    """Stop managing now: no further change is offered or sent. Returns what to restore."""
    with _LOCK:
        record = read()
        if record is None:
            return None
        stopped = replace(record, active=False)
        _write(stopped)
        choose_use_mine()
        return stopped


def restore_failed(*, global_restored: bool = False, models_restored: tuple[str, ...] = ()) -> None:
    """A restore didn't finish: keep what is still to put back, marked, so Settings offers Try again."""
    with _LOCK:
        record = read()
        if record is not None:
            _write(replace(
                record,
                originals_global={} if global_restored else record.originals_global,
                originals_models={key: values for key, values in record.originals_models.items()
                                  if key not in models_restored},
                restore_failed=True,
            ))


def finish_stop() -> None:
    """Every value is back: forget the record."""
    record_path().unlink(missing_ok=True)
