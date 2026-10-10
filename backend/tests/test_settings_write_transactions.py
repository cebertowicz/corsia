"""Settings changes own the read/merge/write and never commit foreign work."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from infra import database, delegation, settings, subagent_caps


def _seed(db):
    for key, value in {
        "probe": {"nested": {"keep": 1, "change": 0}, "other": True},
        "delegation": {"kind_defaults": {"worker": "retired"}, "other": True},
        "agents": {"defaults": {"model": "retired", "temperature": 0.2}},
        "subagents": {"other": True},
    }.items():
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES (?, ?, 't')", (key, json.dumps(value)),
        )
    db.commit()


def _stored(path, key):
    with closing(sqlite3.connect(path, timeout=0.05)) as observer:
        row = observer.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _operate(operation):
    if operation == "replace":
        return settings.set_setting("probe", {"replacement": True})
    if operation == "merge":
        return settings.save_setting_merge("probe", {"nested": {"change": 2}})
    if operation == "delegation":
        return delegation._drop_stale_delegation_defaults()
    if operation == "agents":
        return delegation._migrate_agents_defaults({"retired"})
    return subagent_caps.seed_subagent_caps()


@pytest.mark.parametrize("operation", ["replace", "merge", "delegation", "agents", "seed"])
def test_settings_writer_rejects_foreign_work(isolated_db, monkeypatch, operation):
    db = database.get_db()
    _seed(db)
    monkeypatch.setattr(subagent_caps, "_load_seed_cap", lambda: 3)
    before = {key: _stored(isolated_db, key) for key in ("probe", "delegation", "agents", "subagents")}
    db.execute("INSERT INTO settings VALUES ('foreign', '{}', 't')")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _operate(operation)
        assert db.in_transaction
        assert _stored(isolated_db, "foreign") is None
        assert {key: _stored(isolated_db, key) for key in before} == before
        assert db.execute("SELECT value FROM settings WHERE key='foreign'").fetchone()[0] == "{}"
    finally:
        db.rollback()


@pytest.mark.parametrize(
    "operation,key,want",
    [
        ("merge", "probe", {"nested": {"keep": 1, "change": 2}, "other": True}),
        ("delegation", "delegation", {"other": True}),
        ("agents", "agents", {"defaults": {"temperature": 0.2}}),
        ("seed", "subagents", {"other": True, "max_concurrent_live": 3}),
    ],
)
def test_settings_merge_owns_read_and_preserves_unrelated_keys(
    isolated_db, monkeypatch, operation, key, want,
):
    db = database.get_db()
    _seed(db)
    monkeypatch.setattr(subagent_caps, "_load_seed_cap", lambda: 3)
    # The cap alone exercises the owned read; the idle fields seed the same way.
    monkeypatch.setattr(subagent_caps, "_load_seed_value", lambda field: None)
    reads_owned = []

    def trace(sql):
        if sql.lstrip().upper().startswith("SELECT VALUE FROM SETTINGS"):
            reads_owned.append(db.in_transaction)

    db.set_trace_callback(trace)
    try:
        _operate(operation)
    finally:
        db.set_trace_callback(None)
    assert reads_owned and all(reads_owned)
    assert _stored(isolated_db, key) == want
    assert not db.in_transaction


def test_named_setting_joins_caller_rollback(isolated_db):
    db = database.get_db()
    _seed(db)
    with pytest.raises(RuntimeError, match="abort owner"):
        with database.write_transaction(db, scope="settings_test_owner"):
            db.execute("INSERT INTO settings VALUES ('foreign', '{}', 't')")
            settings.set_setting("probe", {"replacement": True}, db=db)
            assert settings.get_setting("probe", db=db) == {"replacement": True}
            assert _stored(isolated_db, "probe") == {"nested": {"keep": 1, "change": 0}, "other": True}
            raise RuntimeError("abort owner")
    assert _stored(isolated_db, "foreign") is None
    assert _stored(isolated_db, "probe") == {"nested": {"keep": 1, "change": 0}, "other": True}


@pytest.mark.parametrize("operation", ["replace", "merge"])
def test_rejected_setting_does_not_leak_partial_json(isolated_db, operation):
    db = database.get_db()
    _seed(db)
    db.execute(
        "CREATE TRIGGER reject_settings AFTER UPDATE ON settings "
        "WHEN NEW.key='probe' BEGIN SELECT RAISE(FAIL, 'reject setting'); END"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="reject setting"):
        _operate(operation)
    assert not db.in_transaction
    db.commit()
    assert _stored(isolated_db, "probe") == {"nested": {"keep": 1, "change": 0}, "other": True}
    with closing(sqlite3.connect(isolated_db, timeout=0.05)) as observer:
        observer.execute("BEGIN IMMEDIATE")
        observer.rollback()


def test_seed_file_read_precedes_owner_and_never_clobbers_cap(isolated_db, monkeypatch):
    db = database.get_db()
    _seed(db)
    db.execute(
        "UPDATE settings SET value=? WHERE key='subagents'", ('{"max_concurrent_live":2}',),
    )
    db.commit()

    def load_seed():
        assert not db.in_transaction
        return 3

    def load_idle_seed(field):
        assert not db.in_transaction
        return None

    monkeypatch.setattr(subagent_caps, "_load_seed_cap", load_seed)
    monkeypatch.setattr(subagent_caps, "_load_seed_value", load_idle_seed)
    assert subagent_caps.seed_subagent_caps() is False
    assert _stored(isolated_db, "subagents") == {"max_concurrent_live": 2}
    assert not db.in_transaction


def test_delisted_file_read_precedes_setting_scrubs(isolated_db, monkeypatch):
    db = database.get_db()
    _seed(db)

    def load_delisted():
        assert not db.in_transaction
        return {"retired"}

    monkeypatch.setattr(delegation, "_load_delisted_models", load_delisted)
    assert delegation.migrate_delisted_models() is True
    assert delegation.migrate_delisted_models() is False
    assert _stored(isolated_db, "delegation") == {"other": True}
    assert _stored(isolated_db, "agents") == {"defaults": {"temperature": 0.2}}
