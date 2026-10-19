"""Schema 94 gives a once-bound Turn's unbound usage rows its one binding (#579)."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database_migrations import MIGRATIONS
from infra.migration_93_to_94_turn_usage_binding_backfill import migrate_93_to_94
from infra.schema import SCHEMA_VERSION
from infra.usage_schema import USAGE_SCHEMA_SQL

_UNBOUND = (None,) * 5


def _binding(tag: str) -> tuple:
    return (f"plan-{tag}", f"node-{tag}", f"attempt-{tag}", f"commitment-{tag}", "execution")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE active_turns (id TEXT PRIMARY KEY, plan_node_id TEXT)")
    db.execute("CREATE TABLE vendor_accounts (id TEXT PRIMARY KEY)")
    db.executescript(USAGE_SCHEMA_SQL)
    return db


def _insert(db, usage_id: str, thread_id: str, turn_id: str | None, binding: tuple) -> None:
    db.execute(
        """INSERT INTO llm_usage (id,thread_id,turn_id,model,source,created_at,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (usage_id, thread_id, turn_id, "m", "conversation", "t", *binding),
    )


def _state(db) -> dict[str, tuple]:
    return {
        row[0]: tuple(row[1:])
        for row in db.execute(
            """SELECT id,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role
               FROM llm_usage"""
        )
    }


def test_v94_is_registered():
    assert SCHEMA_VERSION == 130
    assert MIGRATIONS[93] is migrate_93_to_94


def test_v94_backfills_only_turns_with_exactly_one_binding_idempotently():
    with closing(_db()) as db:
        # Mixed Turn: unbound rows before and after one bound row.
        _insert(db, "mixed-1", "t1", "turn-mixed", _UNBOUND)
        _insert(db, "mixed-2", "t1", "turn-mixed", _binding("a"))
        _insert(db, "mixed-3", "t1", "turn-mixed", _UNBOUND)
        # Two distinct bindings: which one an unbound row belongs to is unknowable.
        _insert(db, "split-1", "t1", "turn-split", _UNBOUND)
        _insert(db, "split-2", "t1", "turn-split", _binding("a"))
        _insert(db, "split-3", "t1", "turn-split", _binding("b"))
        # Nothing bound in the Turn.
        _insert(db, "free-1", "t1", "turn-free", _UNBOUND)
        # Outside any Turn.
        _insert(db, "null-1", "t1", None, _UNBOUND)
        _insert(db, "null-2", "t1", None, _binding("a"))
        # Same turn_id value on another thread, bound differently.
        _insert(db, "other-1", "t2", "turn-mixed", _UNBOUND)
        _insert(db, "other-2", "t2", "turn-mixed", _binding("z"))
        _insert(db, "other-3", "t3", "turn-mixed", _UNBOUND)
        db.commit()

        migrate_93_to_94(db)
        after_first = _state(db)
        migrate_93_to_94(db)

        assert _state(db) == after_first
        assert after_first == {
            "mixed-1": _binding("a"),
            "mixed-2": _binding("a"),
            "mixed-3": _binding("a"),
            "split-1": _UNBOUND,
            "split-2": _binding("a"),
            "split-3": _binding("b"),
            "free-1": _UNBOUND,
            "null-1": _UNBOUND,
            "null-2": _binding("a"),
            "other-1": _binding("z"),
            "other-2": _binding("z"),
            "other-3": _UNBOUND,
        }
        assert not db.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name LIKE '_usage_backfill%'"
        ).fetchall()


def test_v94_logs_the_rows_and_turns_it_repaired(caplog):
    with closing(_db()) as db:
        _insert(db, "a", "t1", "turn-1", _UNBOUND)
        _insert(db, "b", "t1", "turn-1", _binding("a"))
        _insert(db, "c", "t1", "turn-2", _UNBOUND)
        _insert(db, "d", "t1", "turn-2", _binding("a"))
        _insert(db, "e", "t1", "turn-2", _binding("b"))
        db.commit()
        with caplog.at_level("INFO", logger="infra.migration_93_to_94_turn_usage_binding_backfill"):
            migrate_93_to_94(db)
            migrate_93_to_94(db)

    messages = [record.getMessage() for record in caplog.records]
    assert "attributed 1 unbound llm_usage rows in 1 Turns" in messages[0]
    assert "left 1 Turns with several distinct bindings" in messages[0]
    assert "attributed 0 unbound llm_usage rows in 0 Turns" in messages[1]


def test_v94_skips_a_database_without_the_usage_ledger():
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_93_to_94(db)
