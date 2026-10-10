#!/usr/bin/env python3
"""Schema converter: rename the session vocabulary to thread.

A conversation has always been one entity with two names. There is no
``threads`` table — ``thread_id`` columns already point at ``sessions(id)`` —
so the codebase carried two words for one thing across 40 tables and thousands
of references. This converts the database to the single canonical name.

    sessions            -> threads
    *session_id columns -> *thread_id

SQLite updates foreign-key references automatically for both ALTER forms
(legacy_alter_table is off by default in 3.25+), so the 14 REFERENCES clauses
follow the rename without being rewritten by hand.

Run with --apply; the default is a dry run that reports what would change.

--apply is atomic. SQLite DDL runs in autocommit unless a transaction is
already open, so ``with conn:`` does not roll DDL back (#1518). The script takes
over transactions instead (``isolation_level=None``), turns foreign keys off
before the transaction (a no-op inside one), opens one ``BEGIN IMMEDIATE``,
re-reads the schema version and re-plans inside it (a second starter blocks,
then stands down), snapshots the still-unchanged database on a separate
connection, applies every statement, runs ``foreign_key_check`` and
``integrity_check`` inside the transaction, and commits only if both pass.
Otherwise it rolls back and the database is unchanged. Copy this shape for any
new converter.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

TABLE_RENAME = ("sessions", "threads")


COLUMN_RENAMES = [
    ("project_repo_policies", "root_session_id", "root_thread_id"),
    ("project_repo_policies", "session_id", "thread_id"),
    ("sessions", "parent_session_id", "parent_thread_id"),
    ("messages", "session_id", "thread_id"),
    ("active_turns", "session_id", "thread_id"),
    ("context_bootloader_events", "actor_session_id", "actor_thread_id"),
    ("projects", "conductor_session_id", "conductor_thread_id"),
    ("llm_usage", "session_id", "thread_id"),
    ("artifact_reconcile_receipts", "session_id", "thread_id"),
    ("doc_comments", "author_session_id", "author_thread_id"),
    # Tables created outside schema.py (host_access_leases.py owns its own DDL)
    ("host_access_action_reviews", "session_id", "thread_id"),
    ("host_access_leases", "root_session_id", "root_thread_id"),
    # Tables introduced by the checkpoint rewrite (#1288); their DDL lives in
    # infra/checkpoints.py and infra/continuity_store.py, not infra/schema.py.
    ("checkpoints", "session_id", "thread_id"),
    ("continuity_store", "session_id", "thread_id"),
    ("continuity_entries", "session_id", "thread_id"),
]

TARGET_SCHEMA_VERSION = 4


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def plan(conn: sqlite3.Connection) -> list[str]:
    """Return the exact statements this database still needs."""
    statements: list[str] = []
    tables = _tables(conn)
    old_table, new_table = TABLE_RENAME

    # Columns first, while the table still has its original name.
    for table, old, new in COLUMN_RENAMES:
        probe = table if table in tables else (
            new_table if table == old_table and new_table in tables else table
        )
        cols = _columns(conn, probe)
        if old in cols and new not in cols:
            statements.append(
                f'ALTER TABLE "{probe}" RENAME COLUMN "{old}" TO "{new}"'
            )

    if old_table in tables and new_table not in tables:
        statements.append(f'ALTER TABLE "{old_table}" RENAME TO "{new_table}"')
    return statements


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".pre-thread-rename.bak")


def _snapshot(path: Path, target: Path) -> None:
    """Copy the database from a separate connection.

    The backup API deadlocks against an open write transaction on the same
    connection, and a separate reader cannot see uncommitted changes, so this
    captures exactly the pre-conversion state.
    """
    source = sqlite3.connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def _failed_checks(conn: sqlite3.Connection) -> str | None:
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        return f"FOREIGN KEY CHECK FAILED after conversion: {broken[:5]}"
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        return f"INTEGRITY CHECK FAILED after conversion: {integrity}"
    return None


def _apply(conn: sqlite3.Connection, path: Path) -> int:
    """Convert inside one transaction; commit only when the checks pass."""
    conn.execute("BEGIN IMMEDIATE")
    backup: Path | None = None
    try:
        statements = plan(conn)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if not statements and version == TARGET_SCHEMA_VERSION:
            conn.execute("ROLLBACK")
            print("already converted")
            return 0
        target = _backup_path(path)
        if target.exists():
            raise RuntimeError(f"backup already exists: {target}")
        _snapshot(path, target)
        backup = target
        print(f"backup: {backup}")
        for statement in statements:
            print("apply: " + statement)
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={TARGET_SCHEMA_VERSION}")
        failure = _failed_checks(conn)
        if failure:
            conn.execute("ROLLBACK")
            backup.unlink(missing_ok=True)
            print(f"\n{failure}\nrolled back; database unchanged")
            return 1
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        # Nothing committed, so the snapshot would only block the next --apply.
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    print("\nconverted; foreign_key_check and integrity_check both pass")
    return 0


def main() -> int:
    """Report, or perform, the session -> thread conversion."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="path to corsia.db")
    parser.add_argument("--apply", action="store_true", help="perform the rename")
    args = parser.parse_args()

    path = Path(args.database).expanduser()
    if not path.is_file():
        print(f"no such database: {path}")
        return 2

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        if args.apply:
            return _apply(conn, path)
        statements = plan(conn)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if not statements and version == TARGET_SCHEMA_VERSION:
            print("already converted")
            return 0
        for statement in statements:
            print("would: " + statement)
        print(f"would: PRAGMA user_version={TARGET_SCHEMA_VERSION}")
        print("\ndry run — re-run with --apply; it takes its own backup first")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
