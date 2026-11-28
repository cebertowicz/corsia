from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from infra import database, db_hold_spans


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


def test_spans_nested_hold_and_empty_hold(tmp_path, caplog, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(database, "time", clock)
    monkeypatch.setattr(db_hold_spans, "time", clock)
    monkeypatch.setattr(database, "WRITE_TRANSACTION_SLOW_SECONDS", 0.01)
    db = sqlite3.connect(tmp_path / "spans.db")
    try:
        with database.write_transaction(db, scope="empty"):
            clock.now = 1
        with database.write_transaction(db, scope="outer"):
            with db_hold_spans.span("digest"):
                clock.now = 2
                with database.write_transaction(db, scope="inner"):
                    with db_hold_spans.span("status"):
                        clock.now = 3
                with db_hold_spans.span("status"):
                    clock.now = 4
        lines = [r.getMessage() for r in caplog.records if "SQLite write transaction held" in r.getMessage()]
        assert len(lines) == 2
        assert "scope=empty held=1.000s spans=- callers=" in lines[0]
        assert "scope=outer held=3.000s spans=digest:1/3.000s,status:2/2.000s callers=" in lines[1]
    finally:
        db.close()


def test_rollback_drops_accumulator_and_outside_span_is_noop(tmp_path, caplog, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(database, "time", clock)
    monkeypatch.setattr(db_hold_spans, "time", clock)
    monkeypatch.setattr(database, "WRITE_TRANSACTION_SLOW_SECONDS", 0.01)
    db = sqlite3.connect(tmp_path / "rollback.db")
    try:
        with pytest.raises(ValueError):
            with database.write_transaction(db, scope="rollback"):
                with db_hold_spans.span("digest"):
                    clock.now = 1
                raise ValueError("rollback")
        with db_hold_spans.span("outside"):
            clock.now = 2
        with database.write_transaction(db, scope="next_hold"):
            clock.now = 3
        lines = [r.getMessage() for r in caplog.records if "SQLite write transaction held" in r.getMessage()]
        assert "spans=digest:1/1.000s" in lines[0]
        assert "spans=- callers=" in lines[1]
    finally:
        db.close()


def test_executor_and_to_thread_do_not_inherit_hold(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(database, "WRITE_TRANSACTION_SLOW_SECONDS", 0)
    db = sqlite3.connect(tmp_path / "threads.db")

    def worker():
        with db_hold_spans.span("foreign"):
            pass

    async def run():
        await asyncio.to_thread(worker)
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            await loop.run_in_executor(pool, worker)

    try:
        with database.write_transaction(db, scope="owner"):
            asyncio.run(run())
        line = next(r.getMessage() for r in caplog.records if "scope=owner" in r.getMessage())
        assert "spans=- callers=" in line
    finally:
        db.close()
