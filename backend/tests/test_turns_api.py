from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _seed_turns(db_path) -> None:
    db = sqlite3.connect(db_path)
    db.executemany(
        "INSERT INTO threads "
        "(id,user_id,title,model,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        [
            ("owner-thread", "owner", "Owner", "test/model", "t", "t"),
            ("other-thread", "other", "Other", "test/model", "t", "t"),
        ],
    )
    db.executemany(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,error,"
        "boot_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("owner-active", "owner-thread", 0, "test/model", "running", "1", None, None, "test"),
            ("other-active", "other-thread", 0, "test/model", "running", "2", None, None, "test"),
            ("owner-done", "owner-thread", 0, "test/model", "completed", "3", "4", None, "test"),
            ("other-done", "other-thread", 0, "test/model", "error", "5", "6", "private error", "test"),
        ],
    )
    db.executemany(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,seq,active_turn_id) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("owner-input", "owner-thread", "user", "owner prompt", "1", 1, "owner-active"),
            ("owner-done-input", "owner-thread", "user", "owner done", "3", 2, "owner-done"),
            ("other-input", "other-thread", "user", "private other prompt", "2", 1, "other-active"),
            ("other-done-input", "other-thread", "user", "private other done", "5", 2, "other-done"),
        ],
    )
    db.commit()
    db.close()


def test_turn_monitoring_routes_are_owner_scoped(isolated_db, monkeypatch):
    from api import turns

    _seed_turns(isolated_db)
    monkeypatch.setattr(
        turns,
        "authenticate",
        lambda token: {"id": "owner"} if token == "owner-token" else None,
    )
    app = FastAPI()
    app.include_router(turns.router)
    client = TestClient(app)

    active = client.get("/api/turns/active", params={"token": "owner-token"})
    assert active.status_code == 200
    assert [row["id"] for row in active.json()["active_turns"]] == ["owner-active"]
