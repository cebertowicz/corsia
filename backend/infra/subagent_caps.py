"""Claude Code idle retirement: how long a finished process stays warm and how many do."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from infra.database import write_transaction
from infra.settings import get_setting, set_setting

log = logging.getLogger(__name__)

SETTINGS_KEY = "subagents"
_SEED_PATH = Path(__file__).parent / "subagent_caps.json"
_IDLE_AFTER_FIELD = "idle_retire_after_seconds"
_MAX_IDLE_FIELD = "max_idle_live"
# Field -> smallest valid value. Zero idle handles is a meaningful bound.
_FIELD_MINIMUMS = {_IDLE_AFTER_FIELD: 1, _MAX_IDLE_FIELD: 0}
# Fields no code reads any more, dropped from the row at boot so none reads as a live setting.
_RETIRED_FIELDS = ("max_concurrent_live",)


def _valid(field: str, value) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= _FIELD_MINIMUMS[field]
    )


def _load_seed_value(field: str) -> int | None:
    """Read one cap field from the JSON seed data file, or None if invalid."""
    if not _SEED_PATH.exists():
        log.warning("subagent_caps: seed file missing at %s", _SEED_PATH)
        return None
    try:
        data = json.loads(_SEED_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("subagent_caps: failed to read seed %s: %s", _SEED_PATH, e)
        return None
    val = data.get(field)
    if _valid(field, val):
        return val
    log.warning("subagent_caps: seed %s missing/invalid in %s", field, _SEED_PATH)
    return None


def _load_seeds() -> dict[str, int]:
    seeds = {field: _load_seed_value(field) for field in _FIELD_MINIMUMS}
    return {field: value for field, value in seeds.items() if value is not None}


def seed_subagent_caps() -> bool:
    """Idempotently ensure the ``subagents`` settings row carries every field.

    Called at boot (``init_db``). Writes each JSON seed field ONLY when that
    field is absent or invalid in the settings row — never clobbers an
    operator edit — and drops retired fields. Returns True if it wrote anything.
    """
    seeds = _load_seeds()
    if not seeds:
        return False
    with write_transaction(None, scope="seed_subagent_caps") as db:
        current = get_setting(SETTINGS_KEY, db=db) or {}
        missing = {
            field: value for field, value in seeds.items()
            if not _valid(field, current.get(field))
        }
        retired = [field for field in _RETIRED_FIELDS if field in current]
        if not missing and not retired:
            return False  # operator (or a prior seed) already set them
        kept = {field: value for field, value in current.items() if field not in retired}
        set_setting(SETTINGS_KEY, {**kept, **missing}, db=db)
    log.info("subagent_caps: seeded %s, dropped %s", missing, retired)
    return True


def _configured(field: str) -> int | None:
    """Resolve one field: settings row, then the JSON seed, else None."""
    configured = (get_setting(SETTINGS_KEY) or {}).get(field)
    if _valid(field, configured):
        return configured
    return _load_seed_value(field)


def get_idle_retire_after_seconds() -> int | None:
    """Seconds an idle Claude Code process stays warm, or None if unconfigured.

    None means no data source is valid; the retirement pass then retires
    nothing rather than inventing a number.
    """
    return _configured(_IDLE_AFTER_FIELD)


def get_max_idle_live() -> int | None:
    """How many idle Claude Code processes stay warm, or None if unconfigured."""
    return _configured(_MAX_IDLE_FIELD)
