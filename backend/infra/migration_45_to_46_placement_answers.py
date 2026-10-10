"""Schema 45 -> 46: every selected placement mark carries the fork's answer.

Schema 40's worker chose a selected lesson's destination itself, so its
selections stored only ``checkpoint:<id>``. The checkpoint fork now answers
where the lesson goes when it selects it, and the worker only acts on that
answer. A selection without one has nothing to act on, so this step returns
each such entry to unmarked, where the next checkpoint reviews it again. A
pending staged ``select`` verdict without an answer, from a fork that captured
before the upgrade and installs at startup, is resolved as a conflict with its
reason instead of installing. The worker no longer calls a model, so the
removed ``agents.defaults.placement_worker_model`` setting is dropped too.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

_NO_ANSWER = "json_extract({column},'$.answer') IS NULL"


def migrate_45_to_46(conn: sqlite3.Connection) -> None:
    """Unmark answerless selections; resolve answerless staged selections."""
    conn.execute(
        "UPDATE continuity_entries SET placement_state=NULL,"
        "placement_revision=NULL,placement_detail=NULL "
        "WHERE placement_state='selected' AND (placement_detail IS NULL "
        "OR NOT json_valid(placement_detail) OR "
        + _NO_ANSWER.format(column="placement_detail") + ")"
    )
    conn.execute(
        "UPDATE checkpoint_staged_edits SET outcome='conflict',"
        "outcome_detail='select verdict without a placement answer',"
        "resolved_at=CURRENT_TIMESTAMP "
        "WHERE operation='review' AND outcome='pending' "
        "AND json_extract(body,'$.verdict')='select' AND "
        + _NO_ANSWER.format(column="body")
    )
    _drop_worker_model_setting(conn)


def _drop_worker_model_setting(conn: sqlite3.Connection) -> None:
    """Remove the retired placement worker model from ``agents.defaults``."""
    row = conn.execute("SELECT value FROM settings WHERE key='agents'").fetchone()
    if row is None:
        return
    try:
        agents = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    if not isinstance(defaults, dict) or "placement_worker_model" not in defaults:
        return
    del defaults["placement_worker_model"]
    conn.execute(
        "UPDATE settings SET value=?,updated_at=? WHERE key='agents'",
        (json.dumps(agents),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
