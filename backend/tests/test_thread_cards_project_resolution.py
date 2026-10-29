"""Card pages reuse one project resolution for scope roots and relative origin."""

import sqlite3

import pytest
from fastapi import HTTPException

from api import thread_cards
from infra.schema import SCHEMA_SQL


@pytest.mark.parametrize("planning_exists", [False, True])
def test_project_scope_is_resolved_once_and_keeps_repo_order(tmp_path, monkeypatch, planning_exists):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id, name, path, created_at, updated_at) "
        "VALUES ('p', 'P', '/fixture/p', 't', 't')"
    )
    planning = tmp_path / "projects" / "p"
    if planning_exists:
        for name in ("zeta", "alpha"):
            repo = planning / "source" / name
            repo.mkdir(parents=True)
            db.execute("INSERT INTO project_repo_policies VALUES ('p', ?, ?, NULL, 1)", (name, str(repo)))
    monkeypatch.setattr(thread_cards.config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(thread_cards, "_ask_cards_by_message", lambda *args: ({}, {}))
    calls = []

    def references(db, text, bases, user_id, **kwargs):
        calls.append((bases, kwargs["origin"]))
        return [], 0

    monkeypatch.setattr(thread_cards, "reference_cards", references)
    queries = []
    db.set_trace_callback(queries.append)
    thread_cards.attach_cards(db, "t", "u", {"project_id": "p"}, [
        {"id": "1", "role": "assistant", "content": "first"},
        {"id": "2", "role": "assistant", "content": "second"},
    ])
    assert len([q for q in queries if "SELECT path, name FROM projects" in q]) == 1
    assert len([q for q in queries if "FROM project_repo_policies WHERE project_id" in q]) == (
        1 if planning_exists else 0
    )
    expected = (
        ([planning / "source" / "alpha", planning / "source" / "zeta", planning.resolve()], planning)
        if planning_exists else ([], None)
    )
    assert calls == [expected, expected]
    db.close()


def test_workspace_fallback_and_missing_project(tmp_path, monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    monkeypatch.setattr(thread_cards.config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(thread_cards, "_ask_cards_by_message", lambda *args: ({}, {}))
    calls = []

    def references(db, text, bases, user_id, **kwargs):
        calls.append((bases, kwargs["origin"]))
        return [], 0

    monkeypatch.setattr(thread_cards, "reference_cards", references)
    rows = [{"id": "1", "role": "assistant", "content": "first"}]
    thread_cards.attach_cards(db, "t", "u", {"project_id": None}, rows)
    assert calls == [([tmp_path.resolve()], tmp_path.resolve())]
    with pytest.raises(HTTPException) as exc:
        thread_cards.attach_cards(db, "t", "u", {"project_id": "missing"}, rows)
    assert exc.value.status_code == 404
    db.close()
