"""Attribute a bound Turn's earlier unbound usage rows to its plan binding (#579)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_TUPLE = (
    "plan_id || char(31) || plan_node_id || char(31) || plan_attempt_id || char(31)"
    " || plan_commitment_id || char(31) || plan_usage_role"
)


_DDL = (
    """CREATE TEMP TABLE _usage_backfill_bound (
           thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
           tuples INTEGER NOT NULL,
           plan_id TEXT, plan_node_id TEXT, plan_attempt_id TEXT,
           plan_commitment_id TEXT, plan_usage_role TEXT,
           PRIMARY KEY (thread_id, turn_id)
       ) WITHOUT ROWID""",
    """CREATE TEMP TABLE _usage_backfill_unbound (
           thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
           PRIMARY KEY (thread_id, turn_id)
       ) WITHOUT ROWID""",
)
_TEMP_TABLES = ("_usage_backfill_bound", "_usage_backfill_unbound")
_SINGLE_BINDING = """
    FROM _usage_backfill_bound AS b
    WHERE b.thread_id = llm_usage.thread_id
      AND b.turn_id = llm_usage.turn_id AND b.tuples = 1"""


def _drop_temp_tables(conn) -> None:
    for name in _TEMP_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS temp.{name}")


def _load_turns(conn) -> None:
    """One pass each: the distinct bindings per Turn, and the Turns with unbound rows."""
    conn.execute(
        f"""INSERT INTO _usage_backfill_bound
            SELECT thread_id, turn_id, COUNT(DISTINCT {_TUPLE}),
                   MIN(plan_id), MIN(plan_node_id), MIN(plan_attempt_id),
                   MIN(plan_commitment_id), MIN(plan_usage_role)
            FROM llm_usage
            WHERE turn_id IS NOT NULL AND plan_id IS NOT NULL
            GROUP BY thread_id, turn_id"""
    )
    conn.execute(
        """INSERT INTO _usage_backfill_unbound
           SELECT DISTINCT thread_id, turn_id FROM llm_usage
           WHERE turn_id IS NOT NULL AND plan_id IS NULL"""
    )


def _count_turns(conn, tuples_condition: str) -> int:
    return conn.execute(
        f"""SELECT COUNT(*) FROM _usage_backfill_unbound AS u
            JOIN _usage_backfill_bound AS b USING (thread_id, turn_id)
            WHERE b.tuples {tuples_condition}"""
    ).fetchone()[0]


def _apply_binding(conn) -> int:
    return conn.execute(
        f"""UPDATE llm_usage
            SET (plan_id, plan_node_id, plan_attempt_id,
                 plan_commitment_id, plan_usage_role) = (
                SELECT b.plan_id, b.plan_node_id, b.plan_attempt_id,
                       b.plan_commitment_id, b.plan_usage_role {_SINGLE_BINDING})
            WHERE turn_id IS NOT NULL AND plan_id IS NULL
              AND EXISTS (SELECT 1 {_SINGLE_BINDING})"""
    ).rowcount


def migrate_93_to_94(conn) -> None:
    """Give unbound rows of a once-bound Turn the Turn's binding.

    Runtime rule (`infra.turn_plan_binding`): a Turn is bound once, and at that
    moment every unbound row of the Turn takes the binding. Rows written before
    that rule existed are repaired the same way, only where the Turn carries
    exactly one distinct binding. A Turn with several distinct bindings is left
    as written and counted: which one an unbound row belongs to is unknowable.
    Rows outside any Turn (``turn_id IS NULL``) are never touched. Only
    ``llm_usage`` is migrated.

    Set-based over temporary per-Turn tables keyed on ``(thread_id, turn_id)``,
    so the ledger is scanned a bounded number of times, and idempotent: a repaired
    row is no longer unbound, so a rerun matches nothing.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "llm_usage" not in tables:
        return
    _drop_temp_tables(conn)
    for ddl in _DDL:
        conn.execute(ddl)
    try:
        _load_turns(conn)
        ambiguous = _count_turns(conn, "> 1")
        turns = _count_turns(conn, "= 1")
        rows = _apply_binding(conn)
    finally:
        _drop_temp_tables(conn)
    log.info(
        "migration 93->94: attributed %d unbound llm_usage rows in %d Turns to their "
        "binding; left %d Turns with several distinct bindings untouched",
        rows, turns, ambiguous,
    )
