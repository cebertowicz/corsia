"""Real-store ownership and failure boundaries for durable publication."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from core.workspace_layout import resolve_thread_working_root
from infra.database import TransactionOwnershipError
from registry import durable_writes, models, service
from tests.test_durable_writes import durable_scope as durable_scope


class ConnectionProbe:
    """Inject one COMMIT failure and observe state before pooled cleanup."""

    def __init__(self, connection, fail_commit=0):
        self.connection = connection
        self.fail_commit = fail_commit
        self.commits = 0
        self.closed_dirty = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        self.commits += 1
        if self.commits == self.fail_commit:
            raise sqlite3.OperationalError("injected publication COMMIT failure")
        self.connection.commit()

    def close(self):
        self.closed_dirty.append(self.connection.in_transaction)
        self.connection.close()


def _source(scope):
    _workspace, project, thread, _db = scope
    source = resolve_thread_working_root(project, thread) / "transaction.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"claimed bytes")
    return source


def _seed_applied(scope):
    _workspace, project, thread, db = scope
    target = project / "shared" / "planning" / "transaction.md"
    target.write_bytes(b"applied bytes")
    db.execute(
        "INSERT INTO durable_write_intents "
        "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
        "target_path,stage_path,description,content_sha256,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "g34-applied", "write", "file_applied", thread, "document",
            "workspace://workspace-stable-id/projects/alpha/shared/planning/transaction.md",
            str(target), str(target.parent / ".corsia-write-g34-applied.stage"),
            "Transaction proof", hashlib.sha256(b"applied bytes").hexdigest(),
            "t", "t",
        ),
    )
    db.commit()
    return target


def _run(scope, operation, source=None):
    workspace, project, thread, _db = scope
    if operation == "recover":
        return durable_writes.recover_write_intents(workspace=workspace)
    kwargs = dict(
        thread_id=thread, project_id="project-alpha", workspace=workspace,
        description="Transaction proof", notify=False,
    )
    if operation == "promote":
        return durable_writes.promote_file(source, "planning", **kwargs)
    return durable_writes.write_text(
        project / "shared" / "planning" / "transaction.md", "new bytes", **kwargs,
    )


@pytest.mark.parametrize("operation", ["write", "promote", "recover"])
def test_unknown_owner_is_preserved_before_any_work(durable_scope, monkeypatch, operation):
    _workspace, project, thread, db = durable_scope
    source = _source(durable_scope) if operation == "promote" else None
    if operation == "recover":
        _seed_applied(durable_scope)
    probe = ConnectionProbe(db)
    monkeypatch.setattr(durable_writes, "get_db", lambda: probe)
    old_title = db.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()[0]
    db.execute("UPDATE threads SET title='foreign pending' WHERE id=?", (thread,))

    with pytest.raises(TransactionOwnershipError):
        _run(durable_scope, operation, source)

    assert db.in_transaction
    assert probe.commits == 0
    assert probe.closed_dirty == []
    assert db.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()[0] == "foreign pending"
    if source is not None:
        assert source.read_bytes() == b"claimed bytes"
    target = project / "shared" / "planning" / "transaction.md"
    assert target.exists() == (operation == "recover")
    db.rollback()
    assert db.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()[0] == old_title


@pytest.mark.parametrize("operation", ["write", "promote", "recover"])
def test_filesystem_work_is_outside_sqlite_writer(durable_scope, monkeypatch, operation):
    _workspace, _project, thread, db = durable_scope
    source = _source(durable_scope) if operation == "promote" else None
    if source is not None:
        service.register_write_strict(thread, str(source), db=db)
    if operation == "recover":
        _seed_applied(durable_scope)
    calls = []

    def guarded(original, name):
        def call(*args, **kwargs):
            calls.append(name)
            assert not db.in_transaction, f"filesystem {name} under SQLite writer"
            return original(*args, **kwargs)
        return call

    for name in ("resolve", "exists", "is_file", "is_symlink", "mkdir", "unlink"):
        monkeypatch.setattr(Path, name, guarded(getattr(Path, name), name))
    for name in (
        "claim_bound_file", "stage_bound_file", "stage_bound_bytes", "copy_bound_file",
        "replace_bound_file", "restore_claimed_file", "_sha256_file",
    ):
        monkeypatch.setattr(durable_writes, name, guarded(getattr(durable_writes, name), name))

    _run(durable_scope, operation, source)

    assert "resolve" in calls
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["write", "recover"])
def test_failed_first_commit_is_rolled_back_before_connection_cleanup(
    durable_scope, monkeypatch, operation,
):
    _workspace, project, _thread, db = durable_scope
    if operation == "recover":
        _seed_applied(durable_scope)
    probe = ConnectionProbe(db, fail_commit=1)
    monkeypatch.setattr(durable_writes, "get_db", lambda: probe)

    with pytest.raises(sqlite3.OperationalError, match="injected publication COMMIT"):
        _run(durable_scope, operation)

    assert probe.closed_dirty == [False]
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == (operation == "recover")
    target = project / "shared" / "planning" / "transaction.md"
    assert target.exists() == (operation == "recover")


@pytest.mark.parametrize("fail_commit", [1, 2, 3, 4])
def test_promotion_commit_failure_preserves_source_and_registry(
    durable_scope, monkeypatch, fail_commit,
):
    _workspace, project, thread, db = durable_scope
    source = _source(durable_scope)
    original = service.register_write_strict(thread, str(source), db=db)
    probe = ConnectionProbe(db, fail_commit=fail_commit)
    monkeypatch.setattr(durable_writes, "get_db", lambda: probe)

    with pytest.raises(sqlite3.OperationalError, match="injected publication COMMIT"):
        _run(durable_scope, "promote", source)

    assert probe.closed_dirty == [False]
    assert source.read_bytes() == b"claimed bytes"
    assert not (project / "shared" / "planning" / source.name).exists()
    assert models.get_artifact(original["id"], db=db)["state"] == "active"
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


@pytest.mark.parametrize("fail_commit", [4, 5])
def test_failed_undo_commit_leaves_recoverable_intent_without_pending_sql(
    durable_scope, monkeypatch, fail_commit,
):
    workspace, project, thread, db = durable_scope
    source = _source(durable_scope)
    original = service.register_write_strict(thread, str(source), db=db)
    probe = ConnectionProbe(db, fail_commit=fail_commit)
    monkeypatch.setattr(durable_writes, "get_db", lambda: probe)
    strict = service.register_write_strict

    def fail_registration(*args, **kwargs):
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(service, "register_write_strict", fail_registration)
    with pytest.raises(sqlite3.OperationalError, match="injected publication COMMIT"):
        _run(durable_scope, "promote", source)

    assert probe.closed_dirty == [False]
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 1
    assert models.get_artifact(original["id"], db=db)["state"] == "active"
    monkeypatch.setattr(service, "register_write_strict", strict)
    durable_writes.recover_write_intents(workspace=workspace)
    target = project / "shared" / "planning" / source.name
    # Before undo is durable, existing recovery completes publication; after it
    # is durable, existing recovery completes undo. Neither path loses bytes.
    retained = target if fail_commit == 4 else source
    assert retained.read_bytes() == b"claimed bytes"
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


@pytest.mark.parametrize("operation,fail_commit", [("write", 4), ("promote", 5), ("recover", 2)])
def test_cleanup_commit_failure_keeps_committed_publication_recoverable(
    durable_scope, monkeypatch, operation, fail_commit,
):
    workspace, project, thread, db = durable_scope
    source = _source(durable_scope) if operation == "promote" else None
    original = service.register_write_strict(thread, str(source), db=db) if source else None
    if operation == "recover":
        _seed_applied(durable_scope)
    probe = ConnectionProbe(db, fail_commit=fail_commit)
    monkeypatch.setattr(durable_writes, "get_db", lambda: probe)

    _run(durable_scope, operation, source)

    assert probe.closed_dirty == [False]
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 1
    target = project / "shared" / "planning" / "transaction.md"
    identity = service.find_file_identity(str(target), db=db)
    assert identity is not None
    if original is not None:
        assert not source.exists()
        # A promoted file keeps its registered identity, so its comments follow it.
        assert identity["id"] == original["id"]
        assert models.get_artifact(original["id"], db=db)["state"] == "active"
    retained = target.read_bytes()
    durable_writes.recover_write_intents(workspace=workspace)
    assert target.read_bytes() == retained
    assert service.find_file_identity(str(target), db=db)["id"] == identity["id"]
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0
