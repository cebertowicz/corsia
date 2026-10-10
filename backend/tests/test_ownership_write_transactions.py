"""Ownership changes, their authority, and audit cannot commit separately."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from infra.database import TransactionOwnershipError, get_db, write_transaction
from parent_review import disposition
from registry import ownership
from tests.registry_helpers import seed_artifact


def _seed(db):
    user_id = str(uuid4())
    for thread_id in ("owner-a", "owner-b", "conductor"):
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES (?,?,'Original','native/test','t','t')", (thread_id, user_id),
        )
    seed_artifact(
        db, artifact_id="owned-item", integration="test", type="document",
        external_id="owned-document", parent_thread_id="owner-a", title="A document",
    )
    db.commit()
    return user_id


def _snapshot(db):
    return tuple(
        [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("artifact_identities", "artifact_registrations", "artifact_registration_events", "messages")
    )


def _change(operation, *, db=None, removal=False):
    args = {
        "integration": "test", "type": "document", "external_id": "owned-document",
        "actor_thread_id": "owner-a", "authority": "current-owner-handoff",
        "reason": "Transfer responsibility", "source_path": "test:ownership",
        "db": db,
    }
    if operation == "rehome":
        args["new_owner_thread_id"] = "owner-b"
    if removal:
        args["removal_event"] = {"actor_kind": "agent"}
    return getattr(ownership, f"{operation}_artifact")(**args)


@pytest.mark.parametrize("operation", ("rehome", "release"))
def test_implicit_ownership_change_preserves_foreign_work(isolated_db, operation):
    db = get_db()
    _seed(db)
    before = _snapshot(db)
    db.execute("UPDATE threads SET title='Pending' WHERE id='owner-a'")
    try:
        with pytest.raises(TransactionOwnershipError):
            _change(operation)
        assert db.in_transaction
        assert db.execute("SELECT title FROM threads WHERE id='owner-a'").fetchone()[0] == "Pending"
        with closing(sqlite3.connect(isolated_db)) as observer:
            assert observer.execute("SELECT title FROM threads WHERE id='owner-a'").fetchone()[0] == "Original"
            assert _snapshot(observer) == before
    finally:
        db.rollback()


@pytest.mark.parametrize("operation", ("rehome", "release"))
def test_named_ownership_change_obeys_outer_rollback(isolated_db, operation):
    db = get_db()
    _seed(db)
    before = _snapshot(db)
    with pytest.raises(RuntimeError, match="abort outer"):
        with write_transaction(db, scope="test_ownership_caller"):
            _change(operation, db=db, removal=True)
            assert db.in_transaction
            raise RuntimeError("abort outer")
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _snapshot(observer) == before


class _FailedCommit(sqlite3.Connection):
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("ownership commit rejected")
        return super().commit()


@pytest.mark.parametrize("operation", ("rehome", "release"))
def test_failed_ownership_commit_releases_writer(isolated_db, operation):
    with closing(sqlite3.connect(isolated_db, factory=_FailedCommit)) as db:
        db.row_factory = sqlite3.Row
        _seed(db)
        before = _snapshot(db)
        db.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="ownership commit rejected"):
            _change(operation, db=db, removal=True)
        assert not db.in_transaction
        with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
            assert _snapshot(observer) == before
            observer.execute("UPDATE threads SET title='Available' WHERE id='owner-a'")
            observer.commit()


@pytest.mark.parametrize("operation", ("rehome", "release"))
@pytest.mark.parametrize("failure", ("audit", "removal"))
def test_failed_ownership_unit_cannot_leak_into_outer_commit(isolated_db, operation, failure):
    db = get_db()
    _seed(db)
    table = "artifact_registration_events" if failure == "audit" else "messages"
    db.execute(
        f"CREATE TRIGGER reject_unit AFTER INSERT ON {table} "
        "BEGIN SELECT RAISE(FAIL, 'ownership unit rejected'); END"
    )
    db.commit()
    before = _snapshot(db)
    with write_transaction(db, scope="test_ownership_caller"):
        db.execute("UPDATE threads SET title='Caller retained' WHERE id='owner-a'")
        with pytest.raises(sqlite3.IntegrityError, match="ownership unit rejected"):
            _change(operation, db=db, removal=True)
        assert _snapshot(db) == before
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _snapshot(observer) == before
        assert observer.execute("SELECT title FROM threads WHERE id='owner-a'").fetchone()[0] == "Caller retained"


@pytest.mark.parametrize("operation", ("rehome", "release"))
def test_ownership_reads_are_protected_until_mutation(isolated_db, monkeypatch, operation):
    db = get_db()
    _seed(db)
    name = "_identity" if operation == "rehome" else "routing_registrations"
    original = getattr(ownership, name)

    def checked(*args, **kwargs):
        connection = args[0] if name == "_identity" else kwargs["db"]
        assert connection.in_transaction, "ownership reads must share the write boundary"
        with closing(sqlite3.connect(isolated_db, timeout=0)) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("UPDATE artifact_registrations SET parent_thread_id='owner-b'")
        return original(*args, **kwargs)

    monkeypatch.setattr(ownership, name, checked)
    _change(operation, db=db)
    assert not db.in_transaction


def _parent_setup(db):
    from infra.conductor import WORKSPACE_SCOPE, set_conductor

    _seed(db)
    set_conductor(WORKSPACE_SCOPE, "conductor", db=db)


def _reroute(db=None):
    return disposition.dispose(
        "owned-item", disposition.REROUTE_OWNER, new_owner="owner-b",
        actor_thread="conductor", authority="conductor", reason="Route to owner", db=db,
    )


def test_parent_reroute_metadata_failure_rolls_back_ownership(isolated_db):
    db = get_db()
    _parent_setup(db)
    db.execute(
        "CREATE TRIGGER reject_reroute AFTER UPDATE OF metadata ON artifact_identities "
        "BEGIN SELECT RAISE(FAIL, 'reroute metadata rejected'); END"
    )
    db.commit()
    before = _snapshot(db)
    with pytest.raises(sqlite3.IntegrityError, match="reroute metadata rejected"):
        _reroute(db)
    assert not db.in_transaction
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _snapshot(observer) == before


def test_parent_reroute_preserves_caller_rollback(isolated_db):
    db = get_db()
    _parent_setup(db)
    before = _snapshot(db)
    with pytest.raises(RuntimeError, match="abort parent"):
        with write_transaction(db, scope="test_parent_owner"):
            result = _reroute(db)
            assert result.routed_to == "owner-b"
            assert db.in_transaction
            raise RuntimeError("abort parent")
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _snapshot(observer) == before


def test_parent_reroute_checks_authority_inside_its_write(isolated_db, monkeypatch):
    db = get_db()
    _parent_setup(db)
    original = disposition._override_authorized

    def checked(actor, identity, connection):
        assert connection.in_transaction, "parent authority must share the reroute write"
        return original(actor, identity, connection)

    monkeypatch.setattr(disposition, "_override_authorized", checked)
    assert _reroute(db).audit["old_owner"] == "owner-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("rehome", "release"))
@pytest.mark.parametrize("surface", ("http", "tool"))
async def test_actual_surface_locks_authority_and_publishes_after_commit(
    isolated_db, monkeypatch, operation, surface,
):
    from api import registry_ownership
    from registry import ownership_authority
    from tools import artifact_ownership

    db = get_db()
    user_id = _seed(db)
    name = f"can_{operation}"
    original = getattr(ownership_authority, name)

    def checked(**kwargs):
        assert kwargs["db"].in_transaction, "surface authority must share the mutation write"
        with closing(sqlite3.connect(isolated_db, timeout=0)) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("UPDATE artifact_registrations SET parent_thread_id='owner-b'")
        return original(**kwargs)

    monkeypatch.setattr(ownership_authority, name, checked)
    monkeypatch.setattr(registry_ownership, name, checked)

    def committed_notice(_ref):
        with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
            rows = observer.execute(
                "SELECT parent_thread_id FROM artifact_registrations "
                "WHERE relationship_state IN ('owner-active','owner-pinned')"
            ).fetchall()
            assert rows == ([("owner-b",)] if operation == "rehome" else [])
            observer.execute("UPDATE threads SET title='Notice after commit' WHERE id='owner-a'")
            observer.commit()

    monkeypatch.setattr(artifact_ownership.service, "notify_changed", committed_notice)
    kwargs = {
        "integration": "test", "type": "document", "external_id": "owned-document",
        "authority": "current-owner-handoff", "reason": "Transfer responsibility",
    }
    if operation == "rehome":
        kwargs["new_owner_thread_id"] = "owner-b"
    if surface == "http":
        monkeypatch.setattr(registry_ownership, "authenticate", lambda _token: {"id": user_id})
        app = FastAPI()
        app.include_router(registry_ownership.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/artifacts/{operation}?token=fixture", json={**kwargs, "actor_thread_id": "owner-a"})
        assert response.status_code == 200, response.text
        result = response.json()
    else:
        tool = (
            artifact_ownership.ArtifactRehomeTool()
            if operation == "rehome"
            else artifact_ownership.ArtifactReleaseTool()
        )
        output = await tool.execute(**kwargs, _thread_id="owner-a")
        assert not output.startswith("Error"), output
        result = json.loads(output)
    assert result["old_owner_thread_id"] == "owner-a"
    assert result["new_owner_thread_id"] == ("owner-b" if operation == "rehome" else None)
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert observer.execute("SELECT title FROM threads WHERE id='owner-a'").fetchone()[0] == "Notice after commit"
        if surface == "tool":
            assert observer.execute("SELECT COUNT(*) FROM messages WHERE json_extract(metadata,'$.type')='artifact_removed'").fetchone()[0] == 1
