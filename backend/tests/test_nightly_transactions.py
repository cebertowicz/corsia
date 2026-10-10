"""Nightly persistence never steals a writer or locks across retention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
from threading import Barrier

import pytest

from infra import database
from jobs import nightly


def _rows(path):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(
            "SELECT run_date,status,result_json FROM fact_extraction_runs ORDER BY run_date"
        ).fetchall()


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.05)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _write(operation):
    if operation == "claim":
        return nightly._claim_today("2026-09-13")
    return nightly._record_maintenance_terminal("2026-09-13", "error", error="sweep failed")


@pytest.mark.parametrize("operation", ["claim", "terminal"])
def test_nightly_writer_preserves_unknown_owner(isolated_db, operation):
    db = database.get_db()
    db.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES ('outer-sentinel','pending','t')"
    )
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _write(operation)
        assert db.in_transaction
        assert _rows(isolated_db) == []
        with closing(sqlite3.connect(str(isolated_db))) as observer:
            assert observer.execute(
                "SELECT value FROM settings WHERE key='outer-sentinel'"
            ).fetchone() is None
        db.commit()
        with closing(sqlite3.connect(str(isolated_db))) as observer:
            assert observer.execute(
                "SELECT value FROM settings WHERE key='outer-sentinel'"
            ).fetchone() == ("pending",)
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_foreign_writer_prevents_retention(isolated_db, monkeypatch):
    swept = []
    monkeypatch.setattr("core.thread_lifecycle.run_lifecycle_retention", lambda: swept.append(True))
    db = database.get_db()
    db.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES ('outer-sentinel','pending','t')"
    )
    try:
        with pytest.raises(database.TransactionOwnershipError):
            await nightly._run_job_safely()
        assert swept == []
        assert db.in_transaction
        assert _rows(isolated_db) == []
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("operation", ["claim", "terminal"])
@pytest.mark.parametrize("failure", ["statement", "commit"])
def test_failed_nightly_write_preserves_run_and_releases_writer(
    isolated_db, monkeypatch, operation, failure,
):
    if operation == "terminal":
        assert nightly._claim_today("2026-09-13")
    expected = [] if operation == "claim" else [("2026-09-13", "running", "{}")]
    if failure == "statement":
        with closing(sqlite3.connect(str(isolated_db))) as db:
            db.execute(
                "CREATE TRIGGER reject_run AFTER INSERT ON fact_extraction_runs "
                "BEGIN SELECT RAISE(FAIL,'run rejected'); END"
            )
            db.commit()
    else:
        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("run rejected")

        def factory():
            return sqlite3.connect(str(isolated_db), factory=FailedCommit)

        monkeypatch.setattr(database, "get_db", factory)
        monkeypatch.setattr(nightly, "get_db", factory)
    with pytest.raises(sqlite3.Error, match="run rejected"):
        _write(operation)
    assert _rows(isolated_db) == expected
    _writer_available(isolated_db)


def test_concurrent_day_claims_have_one_durable_winner(isolated_db):
    ready = Barrier(2)

    def claim():
        try:
            ready.wait(timeout=5)
            return nightly._claim_today("2026-09-13")
        finally:
            database.close_thread_db()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(claim)
        second = workers.submit(claim)
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [False, True]
    assert _rows(isolated_db) == [("2026-09-13", "running", "{}")]
    _writer_available(isolated_db)


@pytest.mark.parametrize("status,error,expected", [
    ("ok", None, {}), ("error", "disk gone", {"error": "disk gone"}),
    ("cancelled", "cancelled", {"error": "cancelled"}),
])
def test_terminal_record_replaces_claim_durably(isolated_db, status, error, expected):
    assert nightly._claim_today("2026-09-13")
    nightly._record_maintenance_terminal("2026-09-13", status, error=error)
    row, = _rows(isolated_db)
    assert row[:2] == ("2026-09-13", status)
    assert json.loads(row[2]) == expected
    _writer_available(isolated_db)


@pytest.mark.asyncio
async def test_retention_runs_between_committed_unlocked_writes(isolated_db, monkeypatch):
    observed = []

    def retention():
        _writer_available(isolated_db)
        observed.append(_rows(isolated_db)[0][1:])

    monkeypatch.setattr("core.thread_lifecycle.run_lifecycle_retention", retention)
    await nightly._run_job_safely()
    assert observed == [("running", "{}")]
    assert _rows(isolated_db)[0][1:] == ("ok", "{}")
    _writer_available(isolated_db)
