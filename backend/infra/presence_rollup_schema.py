"""Storage for the focus-attention rollup Measurement reads in place of the raw spans.

Attention is the union of every focused span, so a user who switches windows a
hundred times a day leaves a hundred spans a day and a read that is linear in
them. The rollup keeps the answer instead, per 15-minute UTC bucket (every
viewer zone's midnight falls on one): each row says how many seconds of that
bucket had exactly one set of threads focused. ``api.presence_rollup`` builds it
and ``api.measurement_presence`` reads it; the tables live here so ``infra``
never imports the Measurement SQL.

It is a cache that is never trusted past what it can prove. ``built_through``
(an exclusive bucket) never passes ``now - GRACE`` or the start of a still-open
visit, so a visit that finishes later lands in the raw tail. A visit written
with an earlier start than ``built_through`` (a migration, a retention prune,
a late restart-exit) moves it back in the writer's own transaction, through the
triggers below, and the background refresh rebuilds forward. Nothing here
rebuilds anything.
"""

from __future__ import annotations

from datetime import timedelta

GRACE = timedelta(hours=2)
#: Rollup rows are keyed by this many minutes; a viewer period must start on one to read them.
BUCKET_MINUTES = 15
#: Names the grouping that built the rows; a changed one makes every read raw until a refresh rebuilds it.
DEFINITION = "v1"


def _floor_bucket(instant: str) -> str:
    """SQL: the 15-minute bucket (``YYYY-MM-DD HH:MM:00``) holding a stored timestamp."""
    stamp = f"datetime({instant})"
    return (f"strftime('%Y-%m-%d %H:',{stamp})||printf('%02d',CAST(strftime('%M',{stamp}) AS INTEGER)"
            f"/{BUCKET_MINUTES}*{BUCKET_MINUTES})||':00'")


def _retreat(row: str) -> str:
    return (f"UPDATE presence_focus_rollup_state SET built_through="
            f"MIN(built_through,{_floor_bucket(f'{row}.entered_at')}) WHERE id=1;")


PRESENCE_ROLLUP_TABLES: dict[str, str] = {
    # One row per (user, bucket, set of focused threads): the seconds that set was exactly what was focused.
    "presence_focus_rollup": """
    CREATE TABLE IF NOT EXISTS presence_focus_rollup (
        user_id TEXT NOT NULL, bucket TEXT NOT NULL, set_id INTEGER NOT NULL, seconds REAL NOT NULL,
        PRIMARY KEY (user_id, bucket, set_id)
    ) WITHOUT ROWID""",
    # A set is the distinct (thread, project) pairs focused at once; a project-level visit has no thread.
    "presence_focus_sets": """
    CREATE TABLE IF NOT EXISTS presence_focus_sets (
        id INTEGER PRIMARY KEY, signature TEXT NOT NULL UNIQUE
    )""",
    "presence_focus_set_members": """
    CREATE TABLE IF NOT EXISTS presence_focus_set_members (
        set_id INTEGER NOT NULL, thread_id TEXT, project_id TEXT
    )""",
    "idx_presence_focus_set_members_set": """
    CREATE INDEX IF NOT EXISTS idx_presence_focus_set_members_set ON presence_focus_set_members(set_id)""",
    "presence_focus_rollup_state": """
    CREATE TABLE IF NOT EXISTS presence_focus_rollup_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), definition TEXT NOT NULL, built_through TEXT NOT NULL
    )""",
}

#: Each moves ``built_through`` back to the bucket of any visit row that changes under it. Row-level writes,
#: so the common case (a finished visit starting after ``built_through``) updates nothing.
PRESENCE_ROLLUP_TRIGGERS: dict[str, str] = {
    "trg_presence_visits_rollup_insert": f"""
    CREATE TRIGGER IF NOT EXISTS trg_presence_visits_rollup_insert AFTER INSERT ON presence_visits
    BEGIN {_retreat("NEW")} END""",
    "trg_presence_visits_rollup_update": f"""
    CREATE TRIGGER IF NOT EXISTS trg_presence_visits_rollup_update AFTER UPDATE ON presence_visits
    BEGIN {_retreat("OLD")} {_retreat("NEW")} END""",
    "trg_presence_visits_rollup_delete": f"""
    CREATE TRIGGER IF NOT EXISTS trg_presence_visits_rollup_delete AFTER DELETE ON presence_visits
    BEGIN {_retreat("OLD")} END""",
}

#: Every statement, in creation order: a migration runs them one by one inside the runner's transaction.
PRESENCE_ROLLUP_STATEMENTS = (*PRESENCE_ROLLUP_TABLES.values(), *PRESENCE_ROLLUP_TRIGGERS.values())
PRESENCE_ROLLUP_SQL = "".join(f"{sql};\n" for sql in PRESENCE_ROLLUP_STATEMENTS)
