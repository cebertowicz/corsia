#!/usr/bin/env python3
"""Import validated outcome size ratings into one explicit CORSIA database.

    size_rating_import.py --db PATH --user ID --project ID|all --agent ID|none --input FILE [--apply]

The input is JSON Lines, one rating per accepted outcome, carrying exactly the
fields in ``core.size_rating.FACT_KEYS``. Every row is checked against the
current canonical outcome (scope, plan, revision, latest evidence event and
source snapshot hash) before anything is written; one bad row refuses the whole
file.

Without ``--apply`` this is a dry run: the database is opened read-only and
query-only, and nothing is written, not even an intake counter. With ``--apply``
all new ratings are written in one transaction that commits only when every row
landed; a rating the node's newest fact already serves is left untouched. There is no
default database: the target is always named. Output is aggregate counts only.
Exit status: 0 on success, 2 when the input is refused.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.size_rating_import import SizeImportRefusal, import_size_ratings  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="the SQLite database to read, and with --apply to write")
    parser.add_argument("--user", required=True, help="the user whose Measurement scope the ratings belong to")
    parser.add_argument("--project", required=True, help="project id, or 'all'")
    parser.add_argument("--agent", required=True, help="active agent thread id, or 'none'")
    parser.add_argument("--input", required=True, help="JSON Lines file of ratings")
    parser.add_argument("--apply", action="store_true", help="write the ratings; without it, a read-only dry run")
    return parser


def _read_rows(path: str) -> list[object]:
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise SizeImportRefusal([f"row {number}: not valid JSON"]) from exc
    return rows


def _connect(path: str, apply: bool) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise SizeImportRefusal([f"database {path} does not exist"])
    if apply:
        db = sqlite3.connect(path, isolation_level=None)
    else:
        db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
        db.execute("PRAGMA query_only=ON")
    db.row_factory = sqlite3.Row
    return db


def main(argv: list[str] | None = None) -> int:
    """Run one dry run or apply and print its aggregate report; return the exit status."""
    args = _parser().parse_args(argv)
    try:
        rows = _read_rows(args.input)
        db = _connect(args.db, args.apply)
    except SizeImportRefusal as exc:
        print(json.dumps({"refused": exc.problems}), file=sys.stderr)
        return 2
    try:
        db.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
        report = import_size_ratings(db, rows, user_id=args.user, project=args.project,
                                     agent=args.agent, apply=args.apply)
        db.execute("COMMIT" if args.apply else "ROLLBACK")
    except SizeImportRefusal as exc:
        db.execute("ROLLBACK")
        print(json.dumps({"refused": exc.problems}), file=sys.stderr)
        return 2
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
