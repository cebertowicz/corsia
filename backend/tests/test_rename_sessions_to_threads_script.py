"""``rename_sessions_to_threads.py --apply`` is atomic: a failure changes nothing."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


def _script():
    path = Path(__file__).parents[2] / "scripts/migrations/rename_sessions_to_threads.py"
    spec = importlib.util.spec_from_file_location("rename_sessions_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT);
        CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id));
        INSERT INTO sessions VALUES ('s1', NULL);
        INSERT INTO messages VALUES ('m1', 's1');
        PRAGMA user_version=3;
        """
    )
    conn.commit()
    conn.close()


def _shape(path: Path) -> tuple[list, int]:
    conn = sqlite3.connect(path)
    try:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        return tables, conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _run(module, monkeypatch, path: Path, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["rename", str(path), *args])
    return module.main()


def test_mid_way_failure_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    module = _script()
    db = tmp_path / "corsia.db"
    _make_db(db)
    before = _shape(db)
    real_plan = module.plan
    monkeypatch.setattr(
        module, "plan", lambda conn: [*real_plan(conn)[:2], 'ALTER TABLE "nope" RENAME TO "x"']
    )

    with pytest.raises(sqlite3.OperationalError):
        _run(module, monkeypatch, db, "--apply")

    assert _shape(db) == before
    assert not module._backup_path(db).exists()


def test_failed_validation_rolls_back_and_reports_unchanged(tmp_path, monkeypatch, capsys):
    module = _script()
    db = tmp_path / "corsia.db"
    _make_db(db)
    before = _shape(db)
    monkeypatch.setattr(module, "_failed_checks", lambda conn: "INTEGRITY CHECK FAILED")

    assert _run(module, monkeypatch, db, "--apply") == 1

    assert "database unchanged" in capsys.readouterr().out
    assert _shape(db) == before
    assert not module._backup_path(db).exists()


def test_apply_converts_takes_pre_conversion_backup_and_is_idempotent(tmp_path, monkeypatch):
    module = _script()
    db = tmp_path / "corsia.db"
    _make_db(db)
    before = _shape(db)

    assert _run(module, monkeypatch, db, "--apply") == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT id FROM threads").fetchall() == [("s1",)]
    assert conn.execute("SELECT thread_id FROM messages").fetchall() == [("s1",)]
    conn.close()
    assert _shape(module._backup_path(db)) == (before[0], 3)
    assert _run(module, monkeypatch, db, "--apply") == 0
