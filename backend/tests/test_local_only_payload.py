"""Local-only state in thread list and single-thread payloads."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest



def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    return db



def _thread(
    db,
    thread_id: str,
    *,
    title: str | None = None,
    user_id: str = "u",
    local_only: int = 0,
    project_id: str | None = None,
    spawned_by: str | None = None,
    parent_thread_id: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO threads("
        "id,user_id,title,local_only,project_id,spawned_by,parent_thread_id,created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (
            thread_id, user_id, title or thread_id, local_only, project_id,
            spawned_by, parent_thread_id, "t", "t",
        ),
    )



def _fields(row: dict) -> tuple[bool, str | None, str | None]:
    return (
        row["local_only_effective"],
        row["local_only_inherited_from_thread_id"],
        row["local_only_inherited_from_title"],
    )



def _single(db, thread_id: str) -> dict:
    from infra.queries import get_thread_for_user

    row = get_thread_for_user(thread_id, "u", db=db)
    assert row is not None
    return row



def test_own_local_only_is_effective_without_an_inherited_source() -> None:
    db = _db()
    _thread(db, "parent", local_only=1)
    _thread(db, "child", local_only=1, spawned_by="parent")

    assert _fields(_single(db, "child")) == (True, None, None)



def test_nearest_local_only_ancestor_is_named() -> None:
    db = _db()
    _thread(db, "root", title="Root", local_only=1)
    _thread(db, "parent", title="Nearest", local_only=1, spawned_by="root")
    _thread(db, "child", spawned_by="parent")

    assert _fields(_single(db, "child")) == (True, "parent", "Nearest")



def test_project_cloud_models_off_is_effective_without_an_inherited_source() -> None:
    db = _db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('p',1)")
    _thread(db, "thread", project_id="p")

    assert _fields(_single(db, "thread")) == (True, None, None)



def test_no_local_only_source_is_not_effective() -> None:
    db = _db()
    _thread(db, "thread")

    assert _fields(_single(db, "thread")) == (False, None, None)



def test_other_users_local_only_ancestor_is_ignored() -> None:
    db = _db()
    _thread(db, "foreign-parent", user_id="other", local_only=1)
    _thread(db, "child", spawned_by="foreign-parent")

    assert _fields(_single(db, "child")) == (False, None, None)



def test_list_and_single_thread_reads_include_local_only_payload_fields(monkeypatch) -> None:
    from api import threads
    from connections import harness_refresh

    db = _db()
    _thread(db, "parent", title="Parent", local_only=1)
    _thread(db, "child", parent_thread_id="parent")

    async def read_inline(fn):
        return fn()

    monkeypatch.setattr(threads, "authenticate", lambda _token: {"id": "u"})
    monkeypatch.setattr(threads, "get_db", lambda: db)
    monkeypatch.setattr(threads, "get_all_coordination_ids", lambda *, db: [])
    monkeypatch.setattr(threads, "run_read", read_inline)
    monkeypatch.setattr(harness_refresh, "schedule_refresh_for_selections", lambda _models: None)

    listed = asyncio.run(threads.list_threads("token"))["threads"]
    assert _fields(next(row for row in listed if row["id"] == "child")) == (
        True, "parent", "Parent",
    )

    single_db = _db()
    _thread(single_db, "parent", title="Parent", local_only=1)
    _thread(single_db, "child", spawned_by="parent")
    assert _fields(_single(single_db, "child")) == (True, "parent", "Parent")
