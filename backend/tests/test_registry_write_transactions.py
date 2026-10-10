"""Registry mutations cannot commit a caller or leak a failed partial write."""

from contextlib import closing, nullcontext
import json
import sqlite3

import pytest

from infra.database import TransactionOwnershipError, get_db, write_transaction
from registry import attention, models, pinning, service
from registry.file_identity import FileIdentity
from registry.registration_writer import RegistrationOrigin
from tests.registry_helpers import make_session, seed_artifact


OPERATIONS = (
    "register", "metadata", "surfacing", "pin", "raise", "clear", "retire",
    "retire_temporary",
)


def _seed(db):
    owner = make_session(db, title="Original")
    seed_artifact(
        db, artifact_id="tx-artifact", integration="test", type="note",
        external_id="tx-item", parent_thread_id=owner,
        metadata='{"attention_state":"deferred"}',
        retention="ephemeral",
    )
    db.commit()
    return owner


def _mutate(operation, owner, db=None):
    if operation == "register":
        return models.register_artifact(
            integration="test", type="note", external_id="new-item",
            parent_thread_id=owner, db=db,
        )
    if operation == "metadata":
        return models.update_metadata("test", "note", "tx-item", {"changed": True}, db=db)
    if operation == "surfacing":
        return models.update_surfacing(
            "tx-artifact", surfacing="quiet", parent_thread_id=owner, db=db,
        )
    if operation == "pin":
        return pinning.update_pinned(
            "tx-artifact", pinned=True, parent_thread_id=owner, db=db,
        )
    if operation == "raise":
        return attention.raise_attention("tx-artifact", db=db)
    if operation == "retire":
        return models.retire("tx-artifact", tombstone=True, db=db)
    if operation == "retire_temporary":
        return models.retire_owned_temporary(owner, db=db)
    return attention.clear_attention("tx-artifact", outcome="dismiss", db=db)


def _unchanged(db):
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 1
    assert db.execute("SELECT state FROM artifact_identities").fetchone()[0] == "active"
    assert db.execute("SELECT COUNT(*) FROM artifact_registrations").fetchone()[0] == 1
    assert json.loads(db.execute("SELECT metadata FROM artifact_identities").fetchone()[0]) == {
        "attention_state": "deferred",
    }
    assert db.execute("SELECT surfacing FROM artifact_registrations").fetchone()[0] == "visible"
    assert db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0


@pytest.mark.parametrize("operation", OPERATIONS)
def test_anonymous_mutation_preserves_foreign_owner(isolated_db, operation):
    db = get_db()
    owner = _seed(db)
    db.execute("UPDATE threads SET title='Pending' WHERE id=?", (owner,))
    try:
        with pytest.raises(TransactionOwnershipError):
            _mutate(operation, owner)
        assert db.in_transaction
        assert db.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "Pending"
        with closing(sqlite3.connect(isolated_db)) as observer:
            _unchanged(observer)
            assert observer.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "Original"
    finally:
        db.rollback()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_named_mutation_obeys_caller_rollback(isolated_db, operation):
    db = get_db()
    owner = _seed(db)
    with pytest.raises(RuntimeError, match="caller failed"):
        with write_transaction(db, scope="registry_test_caller"):
            _mutate(operation, owner, db)
            assert db.in_transaction
            raise RuntimeError("caller failed")
    with closing(sqlite3.connect(isolated_db)) as observer:
        _unchanged(observer)


@pytest.mark.parametrize("borrowed", (False, True))
@pytest.mark.parametrize("operation", OPERATIONS)
def test_caught_partial_mutation_cannot_leak_into_later_commit(isolated_db, operation, borrowed):
    db = get_db()
    owner = _seed(db)
    table, verb = {
        "register": ("artifact_registration_events", "INSERT"),
        "metadata": ("artifact_identities", "UPDATE"),
        "surfacing": ("artifact_registration_events", "INSERT"),
        "pin": ("artifact_registration_events", "INSERT"),
        "raise": ("artifact_identities", "UPDATE"),
        "clear": ("context_change_journal", "INSERT"),
        "retire": ("artifact_identities", "UPDATE"),
        "retire_temporary": ("artifact_identities", "UPDATE"),
    }[operation]
    db.execute(
        f"CREATE TRIGGER reject_registry AFTER {verb} ON {table} "
        "BEGIN SELECT RAISE(FAIL, 'registry write rejected'); END"
    )
    db.commit()
    caller = write_transaction(db, scope="registry_test_caller") if borrowed else nullcontext(db)
    with caller:
        if borrowed:
            db.execute("UPDATE threads SET title='Caller retained' WHERE id=?", (owner,))
        with pytest.raises(sqlite3.IntegrityError, match="registry write rejected"):
            _mutate(operation, owner, db)
        assert db.in_transaction is borrowed
        _unchanged(db)
    with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
        _unchanged(observer)
        assert observer.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == (
            "Caller retained" if borrowed else "Original"
        )
        observer.execute("UPDATE threads SET title='Writer released' WHERE id=?", (owner,))
        observer.commit()


class _FailedCommit(sqlite3.Connection):
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("registry commit rejected")
        return super().commit()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_failed_commit_rolls_back_complete_mutation(isolated_db, operation):
    with closing(sqlite3.connect(isolated_db, factory=_FailedCommit)) as db:
        db.row_factory = sqlite3.Row
        owner = _seed(db)
        db.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="registry commit rejected"):
            _mutate(operation, owner, db)
        assert not db.in_transaction
        with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
            _unchanged(observer)
            observer.execute("UPDATE threads SET title='Writer released' WHERE id=?", (owner,))
            observer.commit()


def test_attention_telemetry_observes_committed_state(isolated_db, monkeypatch):
    db = get_db()
    _seed(db)
    observed = []

    def capture(category, action, *, properties):
        with closing(sqlite3.connect(isolated_db)) as observer:
            metadata = json.loads(observer.execute("SELECT metadata FROM artifact_identities").fetchone()[0])
        observed.append((category, action, db.in_transaction, metadata["attention_state"]))

    monkeypatch.setattr(attention, "emit_product_event", capture)
    attention.clear_attention("tx-artifact", outcome="dismiss", db=db)
    assert observed == [("artifact_attention", "cleared", False, "cleared")]
    with pytest.raises(RuntimeError, match="caller failed"):
        with write_transaction(db, scope="registry_test_caller"):
            attention.clear_attention("tx-artifact", outcome="defer", condition={"type": "event", "value": "ready"}, db=db)
            raise RuntimeError("caller failed")
    assert observed == [("artifact_attention", "cleared", False, "cleared")]


def test_strict_registration_rejects_anonymous_foreign_owner_before_path_work(isolated_db, monkeypatch):
    db = get_db()
    owner = _seed(db)
    db.execute("UPDATE threads SET title='Pending' WHERE id=?", (owner,))
    visited = []
    original = service.prepare_file_identity

    def prepare(path, *, db=None):
        visited.append(path)
        return original(path, db=db)

    monkeypatch.setattr(service, "prepare_file_identity", prepare)
    try:
        with pytest.raises(TransactionOwnershipError):
            service.register_write_strict(owner, "note.md")
        assert visited == []
        assert db.in_transaction
    finally:
        db.rollback()


def test_observed_released_artifact_preserves_origin_without_reclaiming_owner(isolated_db, monkeypatch):
    from registry import origin_telemetry

    db = get_db()
    owner = _seed(db)
    models.release_artifact(
        integration="test", type="note", external_id="tx-item",
        actor_thread_id=owner, authority="current-owner-handoff", reason="Released",
        source_path="test", db=db,
    )
    events = []
    monkeypatch.setattr(origin_telemetry, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    result = service.register_observed_artifact(
        owner, integration="test", type="note", external_id="tx-item",
        origin=RegistrationOrigin(owner, "unknown-turn", "observed-tool"),
        db=db, notify=False,
    )
    assert result is None
    assert len(events) == 1
    assert events[0][0] == ("artifact_origin", "recorded")
    assert models.resolve("test", "note", "tx-item", db=db) is None
    assert models.latest_released_owner_thread_id("tx-artifact", db=db) == owner


@pytest.mark.parametrize("borrowed", (False, True))
def test_legacy_file_identity_and_registration_rollback_together(isolated_db, tmp_path, borrowed):
    db = get_db()
    owner = _seed(db)
    canonical = str(tmp_path / "legacy.md")
    seed_artifact(
        db, artifact_id="legacy-artifact", integration="workspace", type="document",
        external_id=canonical, path=canonical, parent_thread_id=owner,
    )
    db.commit()
    db.execute(
        "CREATE TRIGGER reject_registration AFTER INSERT ON artifact_registration_events "
        "BEGIN SELECT RAISE(FAIL, 'registration audit rejected'); END"
    )
    db.commit()
    identity = FileIdentity(canonical, "workspace", False, "document", "workspace://fixture/legacy.md")
    caller = write_transaction(db, scope="registry_test_caller") if borrowed else nullcontext(db)
    with caller:
        with pytest.raises(sqlite3.IntegrityError, match="registration audit rejected"):
            service.register_write_strict(owner, identity, db=db)
        assert db.in_transaction is borrowed
        assert db.execute("SELECT external_id FROM artifact_identities WHERE id='legacy-artifact'").fetchone()[0] == canonical
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert observer.execute("SELECT external_id FROM artifact_identities WHERE id='legacy-artifact'").fetchone()[0] == canonical
        assert observer.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == 0
