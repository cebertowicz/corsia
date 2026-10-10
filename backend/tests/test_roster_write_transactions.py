"""Roster producers protect their before-row and commit it with its journal."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def owner(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,title,model,agent_enabled,agent_status,"
            "agent_responsibility,created_at,updated_at) VALUES "
            "('owner','user','New Chat','native/test',1,'active',"
            "'Owns releases.','t','t')"
        )
        db.commit()
    finally:
        db.close()
    return isolated_db


async def _change(operation, monkeypatch, events):
    from api.agents import update_agent
    from ws.post_turn import ensure_thread_title

    async def title(_recent, _model, *, thread_id=None):
        return "Release owner"

    async def send(message):
        events.append(message)

    if operation == "agent":
        return await update_agent(
            "owner", {"responsibility": "Owns delivery."}, {"id": "user"},
        )
    monkeypatch.setattr("ws.post_turn.generate_thread_title", title)
    return await ensure_thread_title("owner", "release", send)


def _snapshot():
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT title,agent_responsibility FROM threads WHERE id='owner'"
        ).fetchone()
        journal = db.execute(
            "SELECT object_id,summary FROM context_change_journal "
            "WHERE object_kind='agent_roster' ORDER BY seq"
        ).fetchall()
        return tuple(row), [tuple(change) for change in journal]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["agent", "title"])
async def test_before_row_is_protected_from_an_independent_writer(
    owner, monkeypatch, operation,
):
    from infra import database

    real_get_db = database.get_db
    protected_reads = []

    class ProbeConnection(database._NoOpClose):
        def execute(self, sql, *args):
            if sql.startswith("SELECT * FROM threads"):
                peer = sqlite3.connect(str(owner), timeout=0)
                try:
                    peer.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as error:
                    assert "locked" in str(error)
                    protected_reads.append(True)
                else:
                    protected_reads.append(False)
                finally:
                    peer.rollback()
                    peer.close()
            return self._conn.execute(sql, *args)

    def get_db():
        return ProbeConnection(real_get_db()._conn)

    monkeypatch.setattr(database, "get_db", get_db)
    monkeypatch.setattr("api.agents.get_db", get_db)
    monkeypatch.setattr("ws.post_turn.get_db", get_db)
    events = []
    await _change(operation, monkeypatch, events)

    assert protected_reads and all(protected_reads)
    row, journal = _snapshot()
    if operation == "agent":
        assert row == ("New Chat", "Owns delivery.")
        assert journal == [
            ("user:user:workspace", "Agent responsibility map updated: New Chat")
        ]
    else:
        assert row == ("Release owner", "Owns releases.")
        assert journal == [
            ("user:user:workspace", "Agent responsibility map updated: Release owner")
        ]
        assert events == [
            {"type": "session_updated", "thread_id": "owner", "title": "Release owner"}
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["agent", "title"])
async def test_journal_failure_rolls_back_the_roster_row(owner, monkeypatch, operation):
    from infra.database import get_db

    db = get_db()
    db.execute(
        "CREATE TRIGGER reject_roster BEFORE INSERT ON context_change_journal "
        "WHEN NEW.object_kind='agent_roster' BEGIN "
        "SELECT RAISE(FAIL, 'journal unavailable'); END"
    )
    db.commit()
    db.close()
    events = []

    if operation == "agent":
        with pytest.raises(sqlite3.IntegrityError, match="journal unavailable"):
            await _change(operation, monkeypatch, events)
    else:
        await _change(operation, monkeypatch, events)

    assert _snapshot() == (("New Chat", "Owns releases."), [])
    assert events == []


@pytest.mark.asyncio
async def test_operator_title_during_generation_is_preserved(owner, monkeypatch):
    from api.thread_mutations import _write_metadata_patch
    from ws.post_turn import ensure_thread_title

    async def title(_recent, _model, *, thread_id=None):
        result = _write_metadata_patch("owner", "user", {"title": "Operator title"}, "t1")
        assert result["status"] == "ok"
        return "Generated title"

    events = []

    async def send(message):
        events.append(message)

    monkeypatch.setattr("ws.post_turn.generate_thread_title", title)
    await ensure_thread_title("owner", "release", send)

    assert _snapshot() == (
        ("Operator title", "Owns releases."),
        [("user:user:workspace", "Agent responsibility map updated: Operator title")],
    )
    assert events == []
