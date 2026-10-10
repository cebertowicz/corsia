"""Claude Code process caps: the working-process ceiling and idle retirement."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from infra.database import write_transaction
from infra.settings import get_setting, set_setting

log = logging.getLogger(__name__)

SETTINGS_KEY = "subagents"
_SEED_PATH = Path(__file__).parent / "subagent_caps.json"
_CAP_FIELD = "max_concurrent_live"
_IDLE_AFTER_FIELD = "idle_retire_after_seconds"
_MAX_IDLE_FIELD = "max_idle_live"
# Field -> smallest valid value. Zero idle handles is a meaningful bound.
_FIELD_MINIMUMS = {_CAP_FIELD: 1, _IDLE_AFTER_FIELD: 1, _MAX_IDLE_FIELD: 0}
# How many working threads a refusal names before summarizing the rest.
_REFUSAL_LIST_LIMIT = 5


def _format_working(working: tuple[tuple[str, str], ...]) -> str:
    named = ", ".join(
        f'{thread_id[:8]} "{title}"'
        for thread_id, title in working[:_REFUSAL_LIST_LIMIT]
    )
    hidden = len(working) - _REFUSAL_LIST_LIMIT
    return f"{named}, and {hidden} more" if hidden > 0 else named


class LiveSpawnCapExceeded(RuntimeError):
    """Too many Claude Code processes are working to admit another spawn.

    Only working processes count (a Turn, admitted tool work, a pending
    process exit, or a transition such as spawn); idle ones never do. The
    tool layer returns this message and the REST/WS layer maps it to HTTP 429.
    """

    def __init__(
        self,
        cap: int,
        active: int,
        working: tuple[tuple[str, str], ...] = (),
    ):
        self.cap = cap
        self.active = active
        self.working = working
        holders = f" Working: {_format_working(working)}." if working else ""
        super().__init__(
            f"Cannot delegate: {active} Claude Code processes are working a "
            f"Turn right now (cap {cap}); idle ones are not counted.{holders} "
            "Retry when one finishes, or do this work inline."
        )


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


def _load_seed_cap() -> int | None:
    """Read ``max_concurrent_live`` from the JSON seed data file, or None."""
    return _load_seed_value(_CAP_FIELD)


def _load_seeds() -> dict[str, int]:
    seeds = {_CAP_FIELD: _load_seed_cap()}
    for field in (_IDLE_AFTER_FIELD, _MAX_IDLE_FIELD):
        seeds[field] = _load_seed_value(field)
    return {field: value for field, value in seeds.items() if value is not None}


def seed_subagent_caps() -> bool:
    """Idempotently ensure the ``subagents`` settings row carries every cap.

    Called at boot (``init_db``). Writes each JSON seed field ONLY when that
    field is absent or invalid in the settings row — never clobbers an
    operator edit. Returns True if it wrote anything.
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
        if not missing:
            return False  # operator (or a prior seed) already set them
        set_setting(SETTINGS_KEY, {**current, **missing}, db=db)
    log.info("subagent_caps: seeded %s", missing)
    return True


def _configured(field: str) -> int | None:
    """Resolve one field: settings row, then the JSON seed, else None."""
    configured = (get_setting(SETTINGS_KEY) or {}).get(field)
    if _valid(field, configured):
        return configured
    return _load_seed_value(field)


def get_max_concurrent_live() -> int:
    """The configured ceiling on Claude Code processes working at once.

    Resolution: the ``subagents`` settings row (operator-changeable, seeded
    from the JSON data file at boot) → the JSON seed directly (settings row
    not yet seeded) → a conservative in-module floor of 1 (a cap is a SAFETY
    control; if every data source is gone we cap hard rather than fail open to
    unbounded fan-out). Always returns a positive int.
    """
    configured = _configured(_CAP_FIELD)
    return configured if configured is not None else 1


def get_idle_retire_after_seconds() -> int | None:
    """Seconds an idle Claude Code process stays warm, or None if unconfigured.

    None means no data source is valid; the retirement pass then retires
    nothing rather than inventing a number.
    """
    return _configured(_IDLE_AFTER_FIELD)


def get_max_idle_live() -> int | None:
    """How many idle Claude Code processes stay warm, or None if unconfigured."""
    return _configured(_MAX_IDLE_FIELD)
