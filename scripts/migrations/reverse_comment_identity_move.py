#!/usr/bin/env python3
"""Undo schema step 199 -> 200: put comment state back on the retired identities it was moved off.

Reads the ``comment_identity_moves`` ledger and restores each row's original values (see
``infra.migration_199_to_200_comments_off_retired_identities.reverse_moves``). The database is named
explicitly; there is no default. It is a dry run unless ``--apply`` is given, and a dry run changes nothing.

Stop the server before ``--apply``: the app would otherwise read the document's comments from the identity the
move chose while rows return to the old one.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))
from infra.migration_199_to_200_comments_off_retired_identities import LEDGER, reverse_moves  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, type=Path, help="the SQLite database file to restore")
    parser.add_argument("--apply", action="store_true", help="commit the restore (default: report only)")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(args.db))
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (LEDGER,)).fetchone():
            print(f"No {LEDGER} table: nothing was moved here.")
            return 0
        pending = conn.execute(f"SELECT table_name, COUNT(*) FROM {LEDGER} GROUP BY table_name").fetchall()
        print("Ledger rows: " + (", ".join(f"{table}={count}" for table, count in pending) or "none"))
        counts = reverse_moves(conn)
        print("Result: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        if args.apply:
            conn.commit()
            print("Applied.")
        else:
            conn.rollback()
            print("Dry run: nothing changed. Re-run with --apply to restore.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
