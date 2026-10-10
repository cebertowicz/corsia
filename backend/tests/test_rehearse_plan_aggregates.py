"""The aggregates rehearsal script reports counts on a copy and never touches the original (#731)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import scripts.rehearse_plan_aggregates as rehearsal
from tests.test_schema_migration_v106 import _legacy
from tests.plan_writer_authorizer import raw_plan_seed


def _seeded(path: Path) -> None:
    """A schema-105 file with the two shapes the refusals stop: failed over open work, settled parent with a child."""
    db, ids = _legacy()
    # Raw writes: the service now refuses both, which is why old data is the only place they live.
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (ids["phase"],))
    db.commit()
    target = sqlite3.connect(path)
    db.backup(target)
    target.close()
    db.close()


def _report(capsys) -> dict[str, str]:
    lines = capsys.readouterr().out.strip().splitlines()
    return dict(line.split(": ", 1) for line in lines)


def test_a_rehearsal_reports_counts_and_leaves_the_database_alone(tmp_path, capsys) -> None:
    path = tmp_path / "copy.db"
    _seeded(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    status = rehearsal.main(["--db", str(path)])

    report = _report(capsys)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 105
    assert status == 0
    assert report["plans checked"] == "3"
    assert report["recount mismatches"] == "0"
    assert report["plan revision/updated_at changed"] == "0"
    assert report["rows whose containerness differs"] == "0"
    assert report["failed nodes with open descendants"] == "1"
    assert report["settled parents with children"] == "1"
    assert report["settled parents with open children"] == "1"
    assert float(report["ladder seconds"]) >= 0
    assert float(report["migration seconds"]) >= 0
    assert [p.name for p in tmp_path.iterdir()] == ["copy.db"]


def test_it_prints_counts_only(tmp_path, capsys) -> None:
    path = tmp_path / "copy.db"
    _seeded(path)

    rehearsal.main(["--db", str(path)])

    out = capsys.readouterr().out
    assert all(line.split(": ", 1)[1].replace(".", "", 1).isdigit() for line in out.splitlines())
    assert "tmp" not in out and "Phase" not in out


def test_no_copy_migrates_the_named_file_in_place(tmp_path, capsys) -> None:
    path = tmp_path / "disposable.db"
    _seeded(path)

    status = rehearsal.main(["--db", str(path), "--no-copy"])

    report = _report(capsys)
    assert status == 0
    assert report["recount mismatches"] == "0"
    assert report["failed nodes with open descendants"] == "1"
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 106


def test_an_older_database_runs_the_ladder_untimed_before_the_timed_step(tmp_path, capsys) -> None:
    path = tmp_path / "older.db"
    _seeded(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=100")
    db.commit()
    db.close()

    status = rehearsal.main(["--db", str(path)])

    report = _report(capsys)
    assert status == 0
    assert report["schema version"] == "100"
    assert float(report["ladder seconds"]) >= 0
    assert float(report["migration seconds"]) >= 0
    assert list(report).index("ladder seconds") < list(report).index("migration seconds")
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 100


def test_it_refuses_a_database_newer_than_schema_101(tmp_path, capsys) -> None:
    path = tmp_path / "other.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=106")
    db.commit()
    db.close()

    assert rehearsal.main(["--db", str(path)]) == 2
    assert _report(capsys) == {"schema version": "106"}
    assert rehearsal.main(["--db", str(path), "--no-copy"]) == 2
    assert rehearsal.main(["--db", str(tmp_path / "missing.db")]) == 2
