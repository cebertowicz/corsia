"""Account capability discovery follows the canonical account reach rules."""
from __future__ import annotations

import json
import sqlite3
import time

from connections.connected_accounts import (
    GOOGLE_PROVIDER, account_reaches_thread, capabilities_reaching_thread, create_account,
)
from infra import state
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from infra.time import utc_now_iso
from tests.registry_helpers import make_session
from tools.registry import ToolRegistry
from tools.tool_surface import pack_index


def test_index_tracks_reach_and_fails_closed(isolated_db, monkeypatch):
    db = get_db()
    now = utc_now_iso()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", ("p", "P", now, now))
    db.commit()
    project = make_session(db, project_id="p", prefix="project")
    outside = make_session(db, prefix="outside")
    token = state._current_thread_id.set(outside)
    try:
        registry = ToolRegistry()
        before = pack_index(registry)
        assert before == pack_index(registry)
        assert "- google_connect:" in before
        assert "- email_search:" not in before
        account = create_account(GOOGLE_PROVIDER, "a@example.test", reach={"mode": "selected", "project_ids": ["p"], "outside_projects": False}, created_by=("system", "test"), db=db)
        assert pack_index(registry) == before
        state._current_thread_id.set(project)
        with_account = pack_index(registry)
        assert "- email_search:" in with_account
        assert "- microsoft_files_list:" in with_account
        assert with_account == pack_index(registry)
        db.execute("UPDATE connected_accounts SET state='signed_out' WHERE id=?", (account["id"],))
        db.commit()
        assert pack_index(registry) == with_account
        with monkeypatch.context() as patch:
            patch.setattr("connections.connected_accounts.capabilities_reaching_thread", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("read failed")))
            assert pack_index(registry) == before
        db.execute("DELETE FROM connected_accounts WHERE id=?", (account["id"],))
        db.commit()
        assert pack_index(registry) == before
        monkeypatch.setattr("infra.database.get_db", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("unavailable")))
        assert pack_index(registry) == before
    finally:
        state._current_thread_id.reset(token)
        db.close()


def test_bulk_reach_matches_individual_checks_and_query_count(isolated_db):
    db = get_db()
    now = utc_now_iso()
    for project in ("a", "b", "gone"):
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", (project, project, now, now))
    db.commit()
    a = make_session(db, project_id="a", prefix="a")
    b = make_session(db, project_id="b", prefix="b")
    outside = make_session(db, prefix="outside")
    gone = make_session(db, project_id="gone", prefix="gone")
    db.execute("UPDATE threads SET parent_thread_id=? WHERE id=?", (a, b))
    specs = [
        {"mode": "all", "project_ids": [], "outside_projects": False},
        {"mode": "selected", "project_ids": ["a"], "outside_projects": False},
        {"mode": "selected", "project_ids": [], "outside_projects": True},
        {"mode": "selected", "project_ids": [], "outside_projects": False},
    ]
    accounts = [create_account(GOOGLE_PROVIDER, f"{i}@example.test", reach=reach, created_by=("system", "test"), db=db) for i, reach in enumerate(specs)]
    for account, capability in zip(accounts, ("mail", "calendar", "outside", "files")):
        account["capabilities"] = [capability]
        db.execute("UPDATE connected_accounts SET capabilities_json=? WHERE id=?", (json.dumps([capability]), account["id"]))
    db.execute("UPDATE connected_accounts SET reach_mode='corsia' WHERE id=?", (accounts[3]["id"],))
    assert set_corsia_agent(outside, db=db)
    db.execute("DELETE FROM projects WHERE id='gone'")
    db.commit()
    try:
        for thread in (a, b, outside, gone, "missing"):
            expected = set()
            for account in accounts:
                if account_reaches_thread(str(account["id"]), thread, db=db):
                    expected.update(account["capabilities"])
            assert capabilities_reaching_thread(thread, db=db) == expected
        statements = []
        db.set_trace_callback(statements.append)
        capabilities_reaching_thread(a, db=db)
        first_count = len(statements)
        db.set_trace_callback(None)
        for i in range(4, 50):
            create_account(GOOGLE_PROVIDER, f"{i}@example.test", reach=specs[i % len(specs)], created_by=("system", "test"), db=db)
        statements.clear()
        db.set_trace_callback(statements.append)
        capabilities_reaching_thread(a, db=db)
        db.set_trace_callback(None)
        assert len(statements) == first_count
        token = state._current_thread_id.set(a)
        try:
            started = time.perf_counter()
            pack_index(ToolRegistry())
            elapsed_ms = (time.perf_counter() - started) * 1000
        finally:
            state._current_thread_id.reset(token)
        print(f"50-account index render: {elapsed_ms:.3f} ms")
    finally:
        db.set_trace_callback(None)
        db.close()
