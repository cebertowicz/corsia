
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from infra import analytics_db, config, product_events  # noqa: E402
from infra.product_events import emit_product_event  # noqa: E402




@pytest.fixture
def tmp_analytics_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "analytics.duckdb"
    monkeypatch.setattr(config, "ANALYTICS_DB_PATH", db_path)
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", db_path)
    product_events._reset_for_tests()
    yield db_path
    product_events._reset_for_tests()




def test_emit_noop_when_disabled_explicitly(tmp_analytics_db, monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")

    assert emit_product_event("test", "disabled", properties={"foo": "bar"}) is False

    assert not tmp_analytics_db.exists()




def test_emit_writes_row_with_full_context(tmp_analytics_db, monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    monkeypatch.setenv("CORSIA_ANALYTICS_PROJECT", "corsia")

    assert emit_product_event(
        "turn", "started",
        thread_id="abc",
        user_id="steve",
        properties={"model": "claude-haiku-4-5", "token_count": 142},
    ) is True

    conn = analytics_db.get_db()
    rows = conn.execute(
        "SELECT category, action, properties, session_id, project_id "
        "FROM product_events"
    ).fetchall()
    assert len(rows) == 1
    category, action, props_json, session_id, project_id = rows[0]
    assert category == "turn"
    assert action == "started"
    assert session_id == "abc"
    assert project_id == "corsia"
    props = json.loads(props_json)
    assert props["project"] == "corsia"
    assert props["thread_id"] == "abc"
    assert props["user_id"] == "steve"
    assert props["model"] == "claude-haiku-4-5"
    assert props["token_count"] == 142


def test_emit_anonymous_session(tmp_analytics_db, monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)

    emit_product_event("session", "anonymous-action")

    conn = analytics_db.get_db()
    rows = conn.execute(
        "SELECT category, action, session_id, project_id FROM product_events"
    ).fetchall()
    assert len(rows) == 1
    category, action, session_id, project_id = rows[0]
    assert category == "session"
    assert action == "anonymous-action"
    assert session_id is None
    assert project_id == "corsia"


def test_emit_respects_project_id_property(tmp_analytics_db, monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)

    emit_product_event(
        "turn", "started",
        thread_id="sess",
        properties={"project_id": "client-acme"},
    )

    conn = analytics_db.get_db()
    rows = conn.execute("SELECT project_id FROM product_events").fetchall()
    assert rows == [("client-acme",)]




def test_emit_fail_silent_on_db_insert_error(tmp_analytics_db, monkeypatch, caplog):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)

    emit_product_event("warmup", "init")

    class _BrokenConn:
        def execute(self, *args, **kwargs):
            raise RuntimeError("simulated duckdb failure")

    monkeypatch.setattr(analytics_db, "get_db", lambda: _BrokenConn())

    with caplog.at_level(logging.DEBUG, logger="infra.product_events"):
        assert emit_product_event("test", "fail-silent", properties={"k": "v"}) is False

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("emit failed" in rec.message for rec in warning_records), (
        f"expected WARNING 'emit failed'; got "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )


def test_emit_fail_silent_on_duckdb_lock_error(tmp_analytics_db, monkeypatch, caplog):
    """Regression #611: duckdb.IOException (lock held by another process) must not propagate."""
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)

    class _LockError(Exception):
        """Simulate duckdb.IOException — not a subclass of OSError."""

    def _raise_lock(*args, **kwargs):
        raise _LockError("IO Error: Could not set lock on file analytics-dev.duckdb")

    monkeypatch.setattr(analytics_db, "get_db", _raise_lock)

    with caplog.at_level(logging.DEBUG, logger="infra.product_events"):
        # Must not raise — analytics is non-load-bearing
        emit_product_event("session", "started", thread_id="s1", user_id="u1")

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("analytics db open failed" in rec.message for rec in warning_records), (
        f"expected WARNING 'analytics db open failed'; got "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )


def test_emit_fail_silent_on_duckdb_missing(tmp_analytics_db, monkeypatch, caplog):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)

    call_count = {"n": 0}

    def _raise_import(*args, **kwargs):
        call_count["n"] += 1
        raise ImportError("duckdb not found")

    monkeypatch.setattr(analytics_db, "get_db", _raise_import)

    with caplog.at_level(logging.DEBUG, logger="infra.product_events"):
        emit_product_event("test", "first")
        emit_product_event("test", "second")
        emit_product_event("test", "third")

    assert call_count["n"] == 3
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    init_warnings = [r for r in warning_records if "duckdb not installed" in r.message]
    assert len(init_warnings) == 1




def test_flush_is_noop(tmp_analytics_db, monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    product_events.flush()
    emit_product_event("warmup", "init")
    product_events.flush()


def test_identify_user_writes_event_row(tmp_analytics_db, monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)

    product_events.identify_user(
        "steve", traits={"plan": "owner", "tz": "America/Los_Angeles"}
    )

    conn = analytics_db.get_db()
    rows = conn.execute(
        "SELECT category, action, properties FROM product_events"
    ).fetchall()
    assert len(rows) == 1
    category, action, props_json = rows[0]
    assert category == "user"
    assert action == "identified"
    props = json.loads(props_json)
    assert props["user_id"] == "steve"
    assert props["plan"] == "owner"
    assert props["tz"] == "America/Los_Angeles"




def test_schema_persists_across_reopen(tmp_analytics_db, monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)

    emit_product_event("test", "first", thread_id="sess-1")
    emit_product_event("test", "second", thread_id="sess-2")

    product_events._reset_for_tests()
    conn = analytics_db.get_db()
    rows = conn.execute(
        "SELECT category, action, session_id FROM product_events ORDER BY ts"
    ).fetchall()
    assert rows == [("test", "first", "sess-1"), ("test", "second", "sess-2")]
