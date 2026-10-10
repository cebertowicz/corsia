#!/usr/bin/env python3
"""Rehearse schema 103 -> 104 (stored plan aggregates, #731) on a copy of a database.

    rehearse_plan_aggregates.py --db PATH [--no-copy]

``PATH`` is opened read-only and copied into a private temporary directory; the
migration runs on the copy and the copy is deleted, so the named database is
never written. It may be at any schema up to 103: the older steps run on the
copy first and are reported as ``ladder seconds``; ``migration seconds`` is the
103 -> 104 step alone. ``--no-copy`` skips the copy for a file the caller already
made disposable (a 7 GB production file); it is then migrated in place. Output is aggregate counts only,
never a row, id, title or path.

What it reports:

* ``plans checked`` / ``recount mismatches``: plans whose stored aggregates differ
  from an independent recount after the migration (the migration itself fails
  on any, so this is 0 unless the rehearsal is the first to find it).
* ``plan revision/updated_at changed``: plans whose ``revision`` or ``updated_at``
  moved (must be 0).
* ``rows whose containerness differs``: live rows whose stored child count
  disagrees with ``node_states.is_container_sql`` (must be 0).
* ``failed nodes with open descendants`` and ``settled parents with children``:
  the shapes the new write refusals stop producing; their size here is how much
  existing data predates the refusals.

Exit status: 0 when every must-be-zero figure is 0, 1 otherwise, 2 when the
database is newer than schema 103.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.database_migrations import MigrationError, migrate_to_current  # noqa: E402
from plans import public  # noqa: E402

_BEFORE = 103  # the schema this rehearses leaving; the ladder up to it runs untimed
_SETTLED = "('accepted','accepted_with_followup','failed','closed')"
_OPEN = "('pending','active','review_requested')"

_BELOW = """
    WITH RECURSIVE below(root, id) AS (
        SELECT id, id FROM plan_nodes WHERE archived_at IS NULL
        UNION ALL
        SELECT b.root, c.id FROM below b JOIN plan_nodes c ON c.parent_id=b.id
         WHERE c.archived_at IS NULL)
"""


def _count(db: sqlite3.Connection, sql: str) -> int:
    return int(db.execute(sql).fetchone()[0])


def shapes_before(db: sqlite3.Connection) -> dict[str, int]:
    """The data shapes the refusals stop producing; read at schema 103, before the step."""
    return {
        "failed nodes with open descendants": _count(db, _BELOW + f"""
            SELECT COUNT(DISTINCT n.id) FROM plan_nodes n
              JOIN below b ON b.root=n.id AND b.id<>n.id
              JOIN plan_nodes d ON d.id=b.id
             WHERE n.state='failed' AND n.archived_at IS NULL AND d.state IN {_OPEN}"""),
        "settled parents with children": _count(db, f"""
            SELECT COUNT(DISTINCT p.id) FROM plan_nodes p
              JOIN plan_nodes c ON c.parent_id=p.id AND c.archived_at IS NULL
             WHERE p.state IN {_SETTLED} AND p.archived_at IS NULL"""),
        "settled parents with open children": _count(db, _BELOW + f"""
            SELECT COUNT(DISTINCT n.id) FROM plan_nodes n
              JOIN below b ON b.root=n.id AND b.id<>n.id
              JOIN plan_nodes d ON d.id=b.id
             WHERE n.state IN {_SETTLED} AND n.archived_at IS NULL AND d.state IN {_OPEN}"""),
    }


def containerness_differs(db: sqlite3.Connection) -> int:
    """Live rows whose stored ``child_in_work`` disagrees with the live container rule."""
    return _count(db, f"""
        SELECT COUNT(*) FROM plan_nodes n
         WHERE n.archived_at IS NULL
           AND ((n.state='pending' AND n.spawned_thread_id IS NULL AND n.child_in_work>0)
                <> {public.container_sql('n')})""")


def _measure(db: sqlite3.Connection, version: int) -> tuple[dict[str, int | float], int]:
    """Run the ladder to 103 untimed, then time 103 -> 104 alone and check the result."""
    report: dict[str, int | float] = {"schema version": version}
    started = time.perf_counter()
    try:
        migrate_to_current(db, _BEFORE)
    except MigrationError as error:
        report["migration failed"] = 1
        print(f"ladder error: {str(error)[:160]}", file=sys.stderr)
        return report, 1
    report["ladder seconds"] = round(time.perf_counter() - started, 2)
    db.row_factory = sqlite3.Row
    report.update(shapes_before(db))
    report["plans"] = _count(db, "SELECT COUNT(*) FROM plans")
    report["plan nodes"] = _count(db, "SELECT COUNT(*) FROM plan_nodes")
    before = {
        row[0]: (row[1], row[2])
        for row in db.execute("SELECT id, revision, updated_at FROM plans")
    }
    started = time.perf_counter()
    try:
        migrate_to_current(db, _BEFORE + 1)
    except MigrationError as error:
        report["migration failed"] = 1
        print(f"migration error: {str(error)[:160]}", file=sys.stderr)
        return report, 1
    report["migration seconds"] = round(time.perf_counter() - started, 2)
    plans = [row[0] for row in db.execute("SELECT id FROM plans")]
    report["plans checked"] = len(plans)
    report["recount mismatches"] = sum(1 for plan in plans if public.plan_aggregate_mismatches(db, plan))
    report["plan revision/updated_at changed"] = sum(
        1 for row in db.execute("SELECT id, revision, updated_at FROM plans")
        if before.get(row[0]) != (row[1], row[2])
    )
    report["rows whose containerness differs"] = containerness_differs(db)
    zero = ("recount mismatches", "plan revision/updated_at changed", "rows whose containerness differs")
    return report, 0 if all(report[name] == 0 for name in zero) else 1


def rehearse(source: Path, *, no_copy: bool = False) -> tuple[dict[str, int | float], int]:
    if no_copy:
        db = sqlite3.connect(source)
        try:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > _BEFORE:
                return {"schema version": version}, 2
            return _measure(db, version)
        finally:
            db.close()
    with tempfile.TemporaryDirectory(prefix="plan-aggregates-rehearsal-") as work:
        copy = Path(work) / "rehearsal.db"
        original = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            version = int(original.execute("PRAGMA user_version").fetchone()[0])
            if version > _BEFORE:
                return {"schema version": version}, 2
            shutil.copyfile(source, copy)  # a quiescent database; the copy is what runs
        finally:
            original.close()
        db = sqlite3.connect(copy)
        try:
            return _measure(db, version)
        finally:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, type=Path, help="database at schema 103 or older")
    parser.add_argument(
        "--no-copy", action="store_true",
        help="run directly on --db, which the caller says is a disposable copy (it is migrated in place)",
    )
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print("database not found", file=sys.stderr)
        return 2
    report, status = rehearse(args.db, no_copy=args.no_copy)
    for name, value in report.items():
        print(f"{name}: {value}")
    if status == 2:
        print(f"newer than schema {_BEFORE}: nothing to rehearse", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
