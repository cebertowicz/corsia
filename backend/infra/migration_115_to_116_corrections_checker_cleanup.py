"""Schema 115 -> 116: drop the retired provider-checker settings for correction recording."""

from __future__ import annotations

import json
import sqlite3

_RETIRED_SETTING_KEYS = ("corrections_recording_manual_request", "corrections_recording_status")
_RETIRED_AGENT_DEFAULT = "corrections_checker_model"


def migrate_115_to_116(conn: sqlite3.Connection) -> None:
    """Delete the model default and the drain settings no code reads any more.

    Earlier correction facts in ``outcome_events`` are untouched: they belong to a
    prior checker version, and readers key on the active version.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone() is None:
        return
    row = conn.execute("SELECT value FROM settings WHERE key='agents'").fetchone()
    if row is not None:
        try:
            agents = json.loads(row[0])
        except (TypeError, ValueError):
            agents = None
        defaults = agents.get("defaults") if isinstance(agents, dict) else None
        if isinstance(defaults, dict) and _RETIRED_AGENT_DEFAULT in defaults:
            agents["defaults"] = {key: value for key, value in defaults.items() if key != _RETIRED_AGENT_DEFAULT}
            conn.execute("UPDATE settings SET value=? WHERE key='agents'", (json.dumps(agents),))
    conn.executemany("DELETE FROM settings WHERE key=?", [(key,) for key in _RETIRED_SETTING_KEYS])
