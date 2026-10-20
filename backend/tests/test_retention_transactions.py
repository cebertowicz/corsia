"""Retention commits irreversible truth before cleanup, never across filesystem work."""

from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3

import pytest

from core import thread_lifecycle as lifecycle
from core import thread_lifecycle_trims as lifecycle_trims
from infra.database import TransactionOwnershipError
from registry import service
from tests.test_thread_lifecycle import (
    CFG, NOW, _add_artifact_identity, _add_artifact_registration, _add_session,
    _db, _peer_db, _thread_root,
)


def _purge_candidate(db, workspace):
    _add_session(db, "owner", state="archived", archived_reason="dormancy",
                 archived_at=NOW - timedelta(days=181),
                 last_activity=NOW - timedelta(days=250))
    root = _thread_root(workspace, "owner")
    (root / "working" / "owned.txt").write_text("retained until physical cleanup")
    return root


def _sweep(db, workspace):
    return lifecycle.run_retention_sweep(db, now=NOW, config=CFG, workspace=workspace)


def _missing(db, workspace, artifact_id):
    path = workspace / "working" / f"{artifact_id}.txt"
    _add_artifact_identity(db, artifact_id, path=path, retention="ephemeral")
    _add_artifact_registration(db, artifact_id, "owner")
    db.commit()
    return path


def _reconcile(db, workspace, *, single=False):
    if single:
        return service.retire_missing_temporary_under(
            workspace / "working", parent_thread_id="owner", db=db,
        )
    return service.retire_missing_temporary_under_roots(
        {"owner": workspace / "working"}, db=db,
    )


def test_working_sweep_batches_retirement_for_all_threads(tmp_path):
    db = _db()
    for thread_id in ("s1", "s2"):
        _add_session(db, thread_id, last_activity=NOW - timedelta(days=1))
        missing = _thread_root(tmp_path, thread_id) / "working" / "missing.txt"
        artifact_id = f"{thread_id}-missing"
        _add_artifact_identity(db, artifact_id, path=missing, retention="ephemeral")
        _add_artifact_registration(db, artifact_id, thread_id)
    db.commit()

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        lifecycle_trims._trim_working_tmp(
            db, NOW, CFG, tmp_path, lifecycle.RetentionReport(),
        )
    finally:
        db.set_trace_callback(None)

    assert [statement for statement in statements
            if statement.strip().upper() == "BEGIN IMMEDIATE"] == ["BEGIN IMMEDIATE"]
    assert dict(db.execute("SELECT id,state FROM artifact_identities")) == {
        "s1-missing": "tombstone", "s2-missing": "tombstone",
    }


@pytest.mark.parametrize("stage", ("resolve", "remove"))
def test_purge_filesystem_observes_committed_truth_and_a_free_writer(tmp_path, monkeypatch, stage):
    path = tmp_path / "lifecycle.sqlite"
    with closing(_db(path)) as db, closing(_peer_db(path)) as peer:
        root = _purge_candidate(db, tmp_path)
        peer.execute("PRAGMA busy_timeout=0")
        observations = []

        def observe():
            assert not db.in_transaction
            assert peer.execute("SELECT lifecycle_state FROM threads WHERE id='owner'").fetchone()[0] == "purged"
            assert peer.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            peer.execute("UPDATE threads SET updated_at='peer wrote' WHERE id='owner'")
            peer.commit()
            observations.append(stage)

        if stage == "resolve":
            original = lifecycle._thread_dir

            def resolve(*args):
                observe()
                return original(*args)

            monkeypatch.setattr(lifecycle, "_thread_dir", resolve)
        else:
            original = lifecycle.shutil.rmtree

            def remove(*args, **kwargs):
                observe()
                return original(*args, **kwargs)

            monkeypatch.setattr(lifecycle.shutil, "rmtree", remove)

        assert _sweep(db, tmp_path).purged == ["owner"]
        assert observations == [stage]
        assert not root.exists()


def test_sweep_rejects_foreign_transaction_before_deleting_files(tmp_path):
    path = tmp_path / "lifecycle.sqlite"
    with closing(_db(path)) as db, closing(_peer_db(path)) as peer:
        root = _purge_candidate(db, tmp_path)
        db.execute("UPDATE threads SET updated_at='foreign pending' WHERE id='owner'")
        try:
            with pytest.raises(TransactionOwnershipError):
                _sweep(db, tmp_path)
            assert db.in_transaction
            assert root.exists()
            assert peer.execute("SELECT lifecycle_state FROM threads WHERE id='owner'").fetchone()[0] == "archived"
            assert db.execute("SELECT updated_at FROM threads WHERE id='owner'").fetchone()[0] == "foreign pending"
        finally:
            db.rollback()


@pytest.mark.parametrize("single", (False, True))
def test_missing_file_scans_do_not_hold_writer_between_artifacts(tmp_path, monkeypatch, single):
    path = tmp_path / "registry.sqlite"
    with closing(_db(path)) as db, closing(_peer_db(path)) as peer:
        missing = {_missing(db, tmp_path, name) for name in ("first", "second")}
        peer.execute("PRAGMA busy_timeout=0")
        checked = []
        original = Path.exists

        def exists(candidate):
            if candidate in missing:
                assert not db.in_transaction
                peer.execute("UPDATE artifact_identities SET title='peer wrote'")
                peer.commit()
                checked.append(candidate)
            return original(candidate)

        monkeypatch.setattr(Path, "exists", exists)
        assert _reconcile(db, tmp_path, single=single) == ["first", "second"]
        assert set(checked) == missing
        assert not db.in_transaction


@pytest.mark.parametrize("single", (False, True))
def test_missing_reconciliation_rejects_foreign_owner_before_path_scan(tmp_path, monkeypatch, single):
    with closing(_db()) as db:
        missing = _missing(db, tmp_path, "missing")
        db.execute("UPDATE artifact_identities SET title='foreign pending'")
        original = Path.exists
        visited = []

        def exists(candidate):
            if candidate == missing:
                visited.append(candidate)
            return original(candidate)

        monkeypatch.setattr(Path, "exists", exists)
        try:
            with pytest.raises(TransactionOwnershipError):
                _reconcile(db, tmp_path, single=single)
            assert visited == []
            assert db.in_transaction
            assert db.execute("SELECT state FROM artifact_identities").fetchone()[0] == "active"
        finally:
            db.rollback()


@pytest.mark.parametrize("race", ("owner", "path", "version", "retention", "state"))
def test_missing_reconciliation_revalidates_current_identity_and_owner(tmp_path, monkeypatch, race):
    path = tmp_path / "registry.sqlite"
    with closing(_db(path)) as db, closing(_peer_db(path)) as peer:
        missing = _missing(db, tmp_path, "missing")
        original = Path.exists
        changed = []

        def exists(candidate):
            result = original(candidate)
            if candidate == missing and not changed:
                sql = {
                    "owner": "UPDATE artifact_registrations SET parent_thread_id='other'",
                    "path": "UPDATE artifact_identities SET path='moved.txt'",
                    "version": "UPDATE artifact_identities SET updated_at='new version'",
                    "retention": "UPDATE artifact_identities SET retention='durable'",
                    "state": "UPDATE artifact_identities SET state='archived'",
                }[race]
                peer.execute(sql)
                peer.commit()
                changed.append(race)
            return result

        monkeypatch.setattr(Path, "exists", exists)
        assert _reconcile(db, tmp_path) == []
        assert changed == [race]
        assert db.execute("SELECT state FROM artifact_identities").fetchone()[0] == (
            "archived" if race == "state" else "active"
        )


@pytest.mark.parametrize("interrupted", (False, True))
def test_same_sweep_finishes_failed_purge_after_connection_restart(tmp_path, monkeypatch, interrupted):
    path = tmp_path / "lifecycle.sqlite"
    original = lifecycle.shutil.rmtree
    with closing(_db(path)) as db:
        root = _purge_candidate(db, tmp_path)
        for name, retention, owner, backing in (
            ("owned", "ephemeral", "owner", root / "working" / "owned.txt"),
            ("pathless", "ephemeral", "owner", None),
            ("durable", "durable", "owner", root / "context" / "note.md"),
            ("foreign", "ephemeral", "other", root / "working" / "foreign.txt"),
        ):
            _add_artifact_identity(db, name, path=backing, retention=retention)
            _add_artifact_registration(db, name, owner)
        db.commit()
        def stop_cleanup(*args, **kwargs):
            if interrupted:
                raise KeyboardInterrupt("interrupted after purge commit")

        monkeypatch.setattr(lifecycle.shutil, "rmtree", stop_cleanup)
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                _sweep(db, tmp_path)
        else:
            assert _sweep(db, tmp_path).purge_cleanup_errors == 1
        assert root.exists()
        assert db.execute("SELECT lifecycle_state FROM threads WHERE id='owner'").fetchone()[0] == "purged"
        assert dict(db.execute("SELECT id,state FROM artifact_identities")) == {
            "owned": "archived", "pathless": "archived", "durable": "active", "foreign": "active",
        }
    monkeypatch.setattr(lifecycle.shutil, "rmtree", original)
    with closing(_peer_db(path)) as reopened:
        assert _sweep(reopened, tmp_path).purged == []
        assert not root.exists()
        assert dict(reopened.execute("SELECT id,state FROM artifact_identities")) == {
            "owned": "tombstone", "pathless": "archived", "durable": "active", "foreign": "active",
        }
        assert reopened.execute("SELECT history_epoch FROM threads WHERE id='owner'").fetchone()[0] == 1


class _FailedCommit:
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def commit(self):
        raise sqlite3.OperationalError("retention commit failed")


def test_failed_purge_commit_preserves_files_history_and_identity(tmp_path):
    path = tmp_path / "lifecycle.sqlite"
    with closing(_db(path)) as db, closing(_peer_db(path)) as peer:
        root = _purge_candidate(db, tmp_path)
        _add_artifact_identity(db, "owned", path=root / "working" / "owned.txt", retention="ephemeral")
        _add_artifact_registration(db, "owned", "owner")
        db.commit()
        with pytest.raises(sqlite3.OperationalError, match="retention commit failed"):
            _sweep(_FailedCommit(db), tmp_path)
        assert not db.in_transaction
        assert (root / "working" / "owned.txt").exists()
        assert peer.execute("SELECT lifecycle_state,history_epoch FROM threads WHERE id='owner'").fetchone()[:] == ("archived", 0)
        assert peer.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert peer.execute("SELECT state FROM artifact_identities").fetchone()[0] == "active"
        peer.execute("UPDATE threads SET updated_at='writer free'")
        peer.commit()


def test_late_missing_retirement_failure_rolls_back_the_whole_batch(tmp_path):
    path = tmp_path / "registry.sqlite"
    with closing(_db(path)) as db, closing(_peer_db(path)) as peer:
        for name in ("first", "second"):
            _missing(db, tmp_path, name)
        db.execute("CREATE TRIGGER reject_second AFTER UPDATE ON artifact_identities "
                   "WHEN new.id='second' BEGIN SELECT RAISE(FAIL, 'second retirement failed'); END")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="second retirement failed"):
            _reconcile(db, tmp_path)
        assert not db.in_transaction
        assert dict(peer.execute("SELECT id,state FROM artifact_identities")) == {
            "first": "active", "second": "active",
        }


@pytest.mark.parametrize("operation", ("cc", "missing_cc", "codex"))
def test_provider_retirement_obeys_caller_rollback(isolated_db, operation):
    from infra.database import get_db, write_transaction
    from subagents import cc_resume_transition, cc_shutdown, codex_runtime
    from tests.registry_helpers import make_session, seed_artifact

    db = get_db()
    owner = make_session(db)
    for name in ("first", "second"):
        seed_artifact(db, artifact_id=name,
                      integration="codex" if operation == "codex" else "claude-code",
                      type="thread" if operation == "codex" else "session",
                      external_id=name, parent_thread_id=owner)
    db.commit()
    with pytest.raises(RuntimeError, match="caller rolled back"):
        with write_transaction(db, scope="retirement_test_caller"):
            if operation == "cc":
                assert cc_shutdown.retire_provider_session(owner, db=db)
            elif operation == "missing_cc":
                from registry import models

                current = models.find_for_thread(
                    owner, "claude-code", "session", db=db,
                )
                assert current is not None
                cc_resume_transition.retire_rejected_provider_session(
                    owner, current["external_id"], db,
                )
            else:
                assert codex_runtime._retire_registered_thread_artifacts(owner, db=db)
            assert db.in_transaction
            assert db.execute("SELECT COUNT(*) FROM artifact_identities WHERE state='archived'").fetchone()[0] > 0
            raise RuntimeError("caller rolled back")
    with closing(sqlite3.connect(isolated_db)) as peer:
        assert dict(peer.execute("SELECT id,state FROM artifact_identities")) == {
            "first": "active", "second": "active",
        }


def test_missing_cc_retirement_keeps_a_replaced_registration(isolated_db):
    from infra.database import get_db
    from registry import models
    from subagents import cc_resume_transition
    from tests.registry_helpers import make_session, seed_artifact

    db = get_db()
    owner = make_session(db)
    seed_artifact(
        db, artifact_id="replacement", integration="claude-code", type="session",
        external_id="new-session", parent_thread_id=owner,
    )
    db.commit()

    assert not cc_resume_transition.retire_rejected_provider_session(
        owner, "missing-session", db,
    )
    current = models.find_for_thread(owner, "claude-code", "session", db=db)
    assert current is not None
    assert current["external_id"] == "new-session"


def test_codex_retirement_batch_does_not_commit_a_successful_prefix(isolated_db):
    from infra.database import get_db
    from subagents import codex_runtime
    from tests.registry_helpers import make_session, seed_artifact

    db = get_db()
    owner = make_session(db)
    for name in ("first", "second", "keep"):
        seed_artifact(db, artifact_id=name, integration="codex", type="thread",
                      external_id=name, parent_thread_id=owner)
    db.commit()
    db.execute("CREATE TRIGGER reject_second AFTER UPDATE ON artifact_identities "
               "WHEN new.id='second' BEGIN SELECT RAISE(FAIL, 'second retirement failed'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="second retirement failed"):
        codex_runtime._retire_registered_thread_artifacts(owner, db=db, keep_thread_id="keep")
    assert not db.in_transaction
    assert dict(db.execute("SELECT id,state FROM artifact_identities")) == {
        "first": "active", "second": "active", "keep": "active",
    }
    db.execute("DROP TRIGGER reject_second")
    db.commit()
    assert codex_runtime._retire_registered_thread_artifacts(owner, db=db, keep_thread_id="keep")
    assert dict(db.execute("SELECT id,state FROM artifact_identities")) == {
        "first": "archived", "second": "archived", "keep": "active",
    }
