"""Local-only state in thread list payloads."""

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
    from infra.local_only import add_local_only_payload_fields

    row = db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    assert row is not None
    payload = dict(row)
    add_local_only_payload_fields([payload], db=db)
    return payload


def test_copied_column_still_names_the_local_only_parent() -> None:
    db = _db()
    _thread(db, "parent", title="Parent", local_only=1)
    _thread(db, "child", local_only=1, spawned_by="parent")

    payload = _single(db, "child")
    assert _fields(payload) == (True, "parent", "Parent")
    assert payload["local_only_cause"] == "inherited"


def _held_by_route(db, thread_id: str) -> bool:
    """Whether turning Local only off is refused as set by a parent."""
    from infra.local_only import local_only_sources

    return thread_id in local_only_sources([thread_id], db=db)


def test_own_setting_after_parent_turned_off_is_unlocked() -> None:
    db = _db()
    _thread(db, "parent", title="Parent", local_only=0)
    _thread(db, "child", local_only=1, spawned_by="parent")

    payload = _single(db, "child")
    assert _fields(payload) == (True, None, None)
    assert payload["local_only_cause"] == "own"
    assert _held_by_route(db, "child") is False


def test_nearest_local_only_ancestor_is_named() -> None:
    db = _db()
    _thread(db, "root", title="Root", local_only=1)
    _thread(db, "parent", title="Nearest", local_only=1, spawned_by="root")
    _thread(db, "child", local_only=1, spawned_by="parent")

    payload = _single(db, "child")
    assert _fields(payload) == (True, "parent", "Nearest")
    assert payload["local_only_cause"] == "inherited"


def test_project_cloud_models_off_is_effective_without_an_inherited_source() -> None:
    db = _db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('p',1)")
    _thread(db, "thread", project_id="p")

    payload = _single(db, "thread")
    assert _fields(payload) == (True, None, None)
    assert payload["local_only_cause"] == "project"


def test_own_setting_stays_effective_when_the_project_is_also_local_only() -> None:
    db = _db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('p',1)")
    _thread(db, "thread", local_only=1, project_id="p")

    payload = _single(db, "thread")
    assert _fields(payload) == (True, None, None)
    assert payload["local_only_cause"] == "own"


def test_no_local_only_source_is_not_effective() -> None:
    db = _db()
    _thread(db, "thread")

    payload = _single(db, "thread")
    assert _fields(payload) == (False, None, None)
    assert payload["local_only_cause"] is None


def test_other_users_local_only_ancestor_is_ignored() -> None:
    db = _db()
    _thread(db, "foreign-parent", user_id="other", local_only=1)
    _thread(db, "child", local_only=1, spawned_by="foreign-parent")

    assert _fields(_single(db, "child")) == (True, None, None)
    assert _held_by_route(db, "child") is False


def test_list_read_includes_local_only_payload_fields(monkeypatch) -> None:
    from api import threads
    from connections import harness_refresh

    db = _db()
    _thread(db, "parent", title="Parent", local_only=1)
    _thread(db, "child", local_only=1, parent_thread_id="parent")

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


def test_cyclic_family_links_are_bounded_and_agree_with_the_route(monkeypatch) -> None:
    from api import threads
    from connections import harness_refresh

    db = _db()
    _thread(db, "a", title="A", parent_thread_id="b")
    _thread(db, "b", title="B", local_only=1, parent_thread_id="a")

    async def read_inline(fn):
        return fn()

    monkeypatch.setattr(threads, "authenticate", lambda _token: {"id": "u"})
    monkeypatch.setattr(threads, "get_db", lambda: db)
    monkeypatch.setattr(threads, "get_all_coordination_ids", lambda *, db: [])
    monkeypatch.setattr(threads, "run_read", read_inline)
    monkeypatch.setattr(harness_refresh, "schedule_refresh_for_selections", lambda _models: None)

    # The list read closes its connection, so ask the route's question first.
    held_by_route = {thread_id: _held_by_route(db, thread_id) for thread_id in ("a", "b")}
    listed = {row["id"]: row for row in asyncio.run(threads.list_threads("token"))["threads"]}
    assert _fields(listed["a"]) == (False, None, None)
    assert _fields(listed["b"]) == (True, None, None)
    for thread_id, held in held_by_route.items():
        assert (listed[thread_id]["local_only_inherited_from_thread_id"] is not None) is held
