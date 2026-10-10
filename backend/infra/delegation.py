"""Delegated-thread model validation and legacy setting cleanup.

Delegated creation requires an explicit nonblank dispatch model. Legacy setting
cleanup stays here until settings retirement; it never participates in creation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from infra.database import write_transaction
from infra.settings import get_setting, set_setting

log = logging.getLogger(__name__)

SETTINGS_KEY = "delegation"
_SEED_PATH = Path(__file__).parent / "delegation_defaults.json"

class DelegationModelRequired(RuntimeError):
    """A delegated-thread creation request omitted a usable model."""


def _load_delisted_models() -> set[str]:
    """Model ids the JSON seed marks as retired/delisted."""
    if not _SEED_PATH.exists():
        return set()
    try:
        data = json.loads(_SEED_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    return {m for m in (data.get("delisted_models") or []) if m}


def is_delisted_model(model: str | None) -> bool:
    """True when ``model`` is a data-configured retired model id/alias."""
    value = (model or "").strip()
    return bool(value and value in _load_delisted_models())


def migrate_delisted_models() -> bool:
    """Scrub settings values that point at delisted models.

    Stale delegation maps and delisted agent defaults are removed as legacy
    settings cleanup. Delegated creation never consults either value.
    """
    delisted = _load_delisted_models()
    if not delisted:
        return False
    changed = _drop_stale_delegation_defaults()
    return _migrate_agents_defaults(delisted) or changed


def _drop_stale_delegation_defaults() -> bool:
    """Remove retired per-kind model defaults from settings."""
    with write_transaction(None, scope="drop_stale_delegation_defaults") as db:
        current = get_setting(SETTINGS_KEY, db=db) or {}
        stale_keys = [
            key for key in ("role" + "_defaults", "kind_defaults")
            if key in current
        ]
        if not stale_keys:
            return False
        merged = {key: value for key, value in current.items() if key not in stale_keys}
        set_setting(SETTINGS_KEY, merged, db=db)
    log.warning(
        "delegation: removed retired per-kind model defaults from settings; "
        "pass an explicit model on every delegation"
    )
    return True


def _migrate_agents_defaults(delisted: set[str]) -> bool:
    """Remove delisted models from legacy agent defaults."""
    with write_transaction(None, scope="migrate_agents_defaults") as db:
        current = get_setting("agents", db=db) or {}
        defaults = dict(current.get("defaults") or {})
        dead = {k: v for k, v in defaults.items() if isinstance(v, str) and v in delisted}
        if not dead:
            return False
        merged = dict(current)
        merged["defaults"] = {k: v for k, v in defaults.items() if k not in dead}
        set_setting("agents", merged, db=db)
    for k, value in dead.items():
        log.error(
            "agents.defaults.%s was the delisted model %r — removed; set a live "
            "model (delegation never reads agents defaults).", k, value,
        )
    return True


def require_explicit_thread_model(passed_model: str | None) -> str:
    """Return the explicit model a newly created delegated thread runs.

    No system default is consulted: an absent or blank model fails before any
    thread exists.
    """
    if passed_model and passed_model.strip():
        return passed_model.strip()
    raise DelegationModelRequired(
        "No model for delegated thread: model is required and was omitted or "
        "blank. Pass an explicit dispatchable model id from the model catalog "
        "and resend."
    )
