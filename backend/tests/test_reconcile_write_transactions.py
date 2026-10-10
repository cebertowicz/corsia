"""Reconciliation never commits partial repair or another caller's work."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.database import TransactionOwnershipError, get_db, write_transaction
from registry import models, reconcile
from tests.registry_helpers import make_session


def _seed(db, tmp_path, *, retention="durable"):
    owner = make_session(db, user_id="fixture-user")
    artifact = models.register_artifact(
        integration="workspace", type="document", external_id=str(tmp_path / "missing.md"),
        parent_thread_id=owner, path=str(tmp_path / "missing.md"),
        title="Missing document", retention=retention, db=db,
    )
    return owner, dict(db.execute(
        "SELECT * FROM artifact_identities WHERE id=?", (artifact["id"],),
    ).fetchone())


def _receipt(db, owner, artifact, *, status="failed"):
    return reconcile._write_receipt(
        db, artifact_id=artifact["id"], identity_version=artifact["updated_at"],
        thread_id=owner, action="skip", status=status, reason="fixture failure",
    )


def _change_receipt(db, owner, artifact, operation, *, known_receipt):
    if operation == "write":
        return _receipt(db, owner, artifact)
    return reconcile._clear_unresolved_receipt(
        db, artifact_id=artifact["id"], identity_version=artifact["updated_at"],
        thread_id=owner, known_receipt=known_receipt,
    )


def _states(db, artifact):
    state = db.execute(
        "SELECT state FROM artifact_identities WHERE id=?", (artifact["id"],),
    ).fetchone()[0]
    receipts = [row[0] for row in db.execute(
        "SELECT status FROM artifact_reconcile_receipts WHERE artifact_id=?",
        (artifact["id"],),
    )]
    return state, receipts


def _reject_applied(db, artifact):
    db.execute(
        "CREATE TABLE rejected_artifact (id TEXT PRIMARY KEY)"
    )
    db.execute("INSERT INTO rejected_artifact VALUES (?)", (artifact["id"],))
    db.execute(
        "CREATE TRIGGER reject_applied BEFORE INSERT ON artifact_reconcile_receipts "
        "WHEN NEW.status='applied' AND NEW.artifact_id IN (SELECT id FROM rejected_artifact) "
        "BEGIN SELECT RAISE(FAIL, 'applied receipt rejected'); END"
    )
    db.commit()


@pytest.mark.parametrize("healthy", (False, True))
def test_apply_refuses_foreign_work_before_filesystem_or_cleanup(
    isolated_db, tmp_path, monkeypatch, healthy,
):
    db = get_db()
    owner, artifact = _seed(db, tmp_path)
    if healthy:
        (tmp_path / "missing.md").write_text("Still present")
        _receipt(db, owner, artifact)
    db.execute("UPDATE threads SET title='Pending caller' WHERE id=?", (owner,))
    scans = []
    real_missing = reconcile._missing_authoritative

    def observe_stat(path):
        scans.append(path)
        return real_missing(path)

    monkeypatch.setattr(reconcile, "_missing_authoritative", observe_stat)
    try:
        with pytest.raises(TransactionOwnershipError):
            reconcile.apply_for_thread(owner)
        assert scans == []
        assert db.in_transaction
        assert db.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "Pending caller"
        with closing(sqlite3.connect(isolated_db)) as observer:
            assert observer.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "owning thread"
            assert _states(observer, artifact) == ("active", ["failed"] if healthy else [])
    finally:
        db.rollback()


@pytest.mark.parametrize("operation", ("write", "clear"))
def test_receipt_helpers_preserve_caller_rollback(isolated_db, tmp_path, operation):
    db = get_db()
    owner, artifact = _seed(db, tmp_path)
    before = ["failed"] if operation == "clear" else []
    known_receipt = _receipt(db, owner, artifact) if before else None
    with pytest.raises(RuntimeError, match="caller failed"):
        with write_transaction(db, scope="caller"):
            db.execute("UPDATE threads SET title='Pending caller' WHERE id=?", (owner,))
            _change_receipt(
                db, owner, artifact, operation, known_receipt=known_receipt,
            )
            assert db.in_transaction
            with closing(sqlite3.connect(isolated_db)) as observer:
                assert _states(observer, artifact) == ("active", before)
            raise RuntimeError("caller failed")
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _states(observer, artifact) == ("active", before)
        assert observer.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "owning thread"


class _FailCommit(sqlite3.Connection):
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("receipt commit failed")
        return super().commit()


@pytest.mark.parametrize("operation", ("write", "clear"))
def test_failed_receipt_commit_releases_writer(isolated_db, tmp_path, operation):
    with closing(sqlite3.connect(isolated_db, factory=_FailCommit)) as db:
        db.row_factory = sqlite3.Row
        owner, artifact = _seed(db, tmp_path)
        before = ["failed"] if operation == "clear" else []
        known_receipt = _receipt(db, owner, artifact) if before else None
        db.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="receipt commit failed"):
            _change_receipt(
                db, owner, artifact, operation, known_receipt=known_receipt,
            )
        assert not db.in_transaction
        with closing(sqlite3.connect(isolated_db, timeout=0)) as observer:
            assert _states(observer, artifact) == ("active", before)
            observer.execute("UPDATE threads SET title='Writer available' WHERE id=?", (owner,))
            observer.commit()


def test_caught_receipt_failure_cannot_leak_into_caller_commit(isolated_db, tmp_path):
    db = get_db()
    owner, artifact = _seed(db, tmp_path)
    db.execute(
        "CREATE TRIGGER reject_receipt AFTER INSERT ON artifact_reconcile_receipts "
        "BEGIN SELECT RAISE(FAIL, 'receipt insert failed'); END"
    )
    db.commit()
    with write_transaction(db, scope="caller"):
        db.execute("UPDATE threads SET title='Caller survives' WHERE id=?", (owner,))
        with pytest.raises(sqlite3.IntegrityError, match="receipt insert failed"):
            _receipt(db, owner, artifact)
        assert _states(db, artifact) == ("active", [])
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _states(observer, artifact) == ("active", [])
        assert observer.execute("SELECT title FROM threads WHERE id=?", (owner,)).fetchone()[0] == "Caller survives"


@pytest.mark.parametrize("retention", ("durable", "ephemeral"))
def test_receipt_failure_rolls_back_retirement_but_not_other_candidates(
    isolated_db, tmp_path, retention,
):
    db = get_db()
    owner, artifact = _seed(db, tmp_path, retention=retention)
    other = models.register_artifact(
        integration="workspace", type="document", external_id=str(tmp_path / "other.md"),
        parent_thread_id=owner, path=str(tmp_path / "other.md"), db=db,
    )
    _reject_applied(db, artifact)
    result = reconcile.apply_for_thread(owner)
    assert result["counts"] == {"applied": 1, "skipped": 0, "failed": 1}
    assert not db.in_transaction
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _states(observer, artifact) == ("active", ["failed"])
        assert _states(observer, other) == ("archived", ["applied"])


@pytest.mark.parametrize("race", ("owner", "version", "applied"))
def test_final_write_revalidates_after_last_filesystem_check(
    isolated_db, tmp_path, monkeypatch, race,
):
    db = get_db()
    owner, artifact = _seed(db, tmp_path)
    checks = []
    real_missing = reconcile._missing_authoritative

    def race_after_stat(path):
        result = real_missing(path)
        checks.append(path)
        if len(checks) == 2:
            with closing(sqlite3.connect(isolated_db, timeout=0)) as contender:
                contender.row_factory = sqlite3.Row
                if race == "owner":
                    contender.execute("DELETE FROM artifact_registrations WHERE artifact_id=?", (artifact["id"],))
                elif race == "version":
                    contender.execute("UPDATE artifact_identities SET updated_at='changed' WHERE id=?", (artifact["id"],))
                else:
                    _receipt(contender, owner, artifact, status="applied")
                contender.commit()
        return result

    monkeypatch.setattr(reconcile, "_missing_authoritative", race_after_stat)
    result = reconcile.apply_for_thread(owner)
    assert len(checks) == 2
    assert result["counts"] == {"applied": 0, "skipped": 0 if race == "applied" else 1, "failed": 0}
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _states(observer, artifact) == ("active", ["applied" if race == "applied" else "skipped"])


def test_filesystem_checks_never_hold_writer(isolated_db, tmp_path, monkeypatch):
    db = get_db()
    owner, artifact = _seed(db, tmp_path)
    real_missing = reconcile._missing_authoritative
    observations = []

    def stat_with_contending_writer(path):
        observations.append(db.in_transaction)
        with closing(sqlite3.connect(isolated_db, timeout=0)) as contender:
            contender.execute("UPDATE threads SET title='Concurrent writer' WHERE id=?", (owner,))
            contender.commit()
        return real_missing(path)

    monkeypatch.setattr(reconcile, "_missing_authoritative", stat_with_contending_writer)
    assert reconcile.apply_for_thread(owner)["counts"]["applied"] == 1
    assert observations == [False, False]
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _states(observer, artifact) == ("archived", ["applied"])


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ("tool", "http"))
@pytest.mark.parametrize("reject_receipt", (False, True))
async def test_actual_reconcile_surfaces_report_only_committed_repairs(
    isolated_db, tmp_path, monkeypatch, surface, reject_receipt,
):
    from api import registry_pane
    from tools.artifacts import ArtifactReconcileTool

    db = get_db()
    owner, artifact = _seed(db, tmp_path)
    if reject_receipt:
        _reject_applied(db, artifact)
    if surface == "tool":
        result = json.loads(await ArtifactReconcileTool().execute(_thread_id=owner, mode="apply"))
    else:
        monkeypatch.setattr(registry_pane, "authenticate", lambda token: {"id": "fixture-user"})
        app = FastAPI()
        app.include_router(registry_pane.router)
        with TestClient(app) as client:
            response = client.post(f"/api/threads/{owner}/artifacts/reconcile?token=fixture-token")
            assert response.status_code == 200
            result = response.json()
    assert result["counts"] == {"applied": 0 if reject_receipt else 1, "failed": 1 if reject_receipt else 0, "skipped": 0}
    with closing(sqlite3.connect(isolated_db)) as observer:
        assert _states(observer, artifact) == (
            "active" if reject_receipt else "archived",
            ["failed" if reject_receipt else "applied"],
        )
