"""Relocation keeps each durable phase without borrowing another writer."""

from contextlib import closing
import json
import sqlite3
import uuid

import httpx
import pytest

from auth_fixtures import seed_account
from fastapi import FastAPI

from infra import auth, config, database
from registry import relocate
from tests.registry_helpers import seed_artifact


@pytest.fixture
def relocation_case(isolated_db):
    # A tmp_path name repeats once pytest removes a passing test's directory,
    # so it cannot name a zone that outlives it in the shared workspace.
    zone = config.WORKSPACE / f"relocation-{uuid.uuid4().hex}"
    zone.mkdir(parents=True)
    source, target = zone / "source.md", zone / "target.py"
    source.write_text("preserved bytes")
    with database.write_transaction(None, scope="relocation_test_seed") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
            "VALUES ('move-owner','fixture-user','Original','t','t')",
        )
        seed_artifact(
            db, artifact_id="moving", integration="workspace", type="document",
            external_id=str(source), parent_thread_id="move-owner",
            title="Custom title", path=str(source),
        )
    return isolated_db, source, target


def _snapshot(path, artifact_id="moving"):
    with closing(sqlite3.connect(str(path))) as db:
        return (
            db.execute("SELECT title FROM threads WHERE id='move-owner'").fetchone()[0],
            db.execute(
                "SELECT path,type,title FROM artifact_identities WHERE id=?", (artifact_id,),
            ).fetchone(),
            db.execute(
                "SELECT phase FROM artifact_relocation_intents WHERE artifact_id=?",
                (artifact_id,),
            ).fetchall(),
            db.execute(
                "SELECT COUNT(*) FROM artifact_registration_events "
                "WHERE artifact_id=? AND event_type='relocated'", (artifact_id,),
            ).fetchone()[0],
        )


def _writer_free(path):
    with closing(sqlite3.connect(str(path), timeout=0)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _seed_intent(db, source, target, *, artifact_id="moving", phase="recorded"):
    db.execute(
        "INSERT INTO artifact_relocation_intents "
        "(id,artifact_id,actor_thread_id,source_path,target_path,source_external_id,"
        "target_external_id,target_type,target_title,phase,created_at,updated_at) "
        "VALUES (?,?,'move-owner',?,?,?,?,'code','Custom title',?,'t','t')",
        (f"intent-{artifact_id}", artifact_id, str(source), str(target), str(source),
         str(target), phase),
    )


@pytest.mark.parametrize("operation", ["relocate", "recover"])
@pytest.mark.parametrize("named", [True, False])
def test_foreign_transaction_is_preserved_before_any_file_move(relocation_case, operation, named):
    path, source, target = relocation_case
    if operation == "recover":
        with database.write_transaction(None, scope="pending_relocation_seed") as db:
            _seed_intent(db, source, target)
    before = _snapshot(path)
    db = database.get_db()
    try:
        db.execute("UPDATE threads SET title='Caller' WHERE id='move-owner'")
        with pytest.raises(database.TransactionOwnershipError):
            if operation == "relocate":
                relocate.relocate_artifact(
                    "moving", str(target), actor_thread_id="move-owner", db=db if named else None,
                )
            else:
                relocate.recover_interrupted_relocations(db=db if named else None)
        assert db.in_transaction
        assert db.execute("SELECT title FROM threads WHERE id='move-owner'").fetchone()[0] == "Caller"
        assert _snapshot(path) == before
        assert source.read_text() == "preserved bytes"
        assert not target.exists()
    finally:
        db.rollback()
        db.close()


class _FailedCommit(sqlite3.Connection):
    count = 0
    fail_at: int | None = None

    def commit(self):
        self.count += 1
        if self.count == self.fail_at:
            raise sqlite3.OperationalError("relocation commit rejected")
        super().commit()


@pytest.mark.parametrize("failed_commit,expected_phase", [
    (1, None), (2, "recorded"), (3, "file_moved"), (4, "identity_updated"),
])
def test_failed_phase_leaves_only_prior_committed_state(
    relocation_case, failed_commit, expected_phase,
):
    path, source, target = relocation_case
    db = sqlite3.connect(str(path), factory=_FailedCommit)
    db.row_factory = sqlite3.Row
    db.fail_at = failed_commit
    try:
        with pytest.raises(sqlite3.OperationalError, match="relocation commit rejected"):
            relocate.relocate_artifact("moving", str(target), actor_thread_id="move-owner", db=db)
        assert not db.in_transaction
        _writer_free(path)
        expected_identity = (
            (str(target), "code", "Custom title") if failed_commit == 4
            else (str(source), "document", "Custom title")
        )
        assert _snapshot(path) == (
            "Original", expected_identity,
            [(expected_phase,)] if expected_phase else [], 0,
        )
        assert source.exists() is (failed_commit == 1)
        assert target.exists() is (failed_commit != 1)
        db.fail_at = None
        recovered = relocate.recover_interrupted_relocations(db=db)
        if failed_commit == 1:
            assert recovered == []
        else:
            assert len(recovered) == 1
            assert _snapshot(path) == (
                "Original", (str(target), "code", "Custom title"), [], 1,
            )
            assert target.read_text() == "preserved bytes"
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["identity", "audit"])
def test_bad_recovery_phase_cannot_leak_into_the_next_intents_commit(relocation_case, failure):
    path, source, target = relocation_case
    source.rename(target)
    other_source, other_target = source.parent / "other.md", source.parent / "other.py"
    other_source.write_text("other bytes")
    phase = "file_moved" if failure == "identity" else "identity_updated"
    with database.write_transaction(None, scope="relocation_recovery_fault_seed") as db:
        _seed_intent(db, source, target, phase=phase)
        if failure == "audit":
            db.execute(
                "UPDATE artifact_identities SET path=?,external_id=?,type='code' WHERE id='moving'",
                (str(target), str(target)),
            )
        seed_artifact(
            db, artifact_id="other", integration="workspace", type="document",
            external_id=str(other_source), parent_thread_id="move-owner",
            title="Custom title", path=str(other_source),
        )
        _seed_intent(db, other_source, other_target, artifact_id="other")
        if failure == "identity":
            db.execute(
                "CREATE TRIGGER reject_phase AFTER UPDATE ON artifact_identities "
                "WHEN NEW.id='moving' BEGIN SELECT RAISE(FAIL, 'phase rejected'); END",
            )
        else:
            db.execute(
                "CREATE TRIGGER reject_phase AFTER INSERT ON artifact_registration_events "
                "WHEN NEW.artifact_id='moving' BEGIN SELECT RAISE(FAIL, 'phase rejected'); END",
            )
    before = _snapshot(path)
    assert relocate.recover_interrupted_relocations() == ["intent-other"]
    assert _snapshot(path) == before
    assert _snapshot(path, "other") == (
        "Original", (str(other_target), "code", "Custom title"), [], 1,
    )
    assert target.read_text() == "preserved bytes"
    assert other_target.read_text() == "other bytes"
    _writer_free(path)


@pytest.mark.parametrize("surface", ["tool", "http"])
@pytest.mark.asyncio
async def test_real_surface_moves_after_durable_intent_with_writer_free(
    relocation_case, monkeypatch, surface,
):
    from api.registry_pane import router
    from tools.artifacts import ArtifactRelocateTool

    path, source, target = relocation_case
    real_rename = relocate.os.rename

    def rename(source_path, target_path):
        assert _snapshot(path)[2] == [("recorded",)]
        _writer_free(path)
        real_rename(source_path, target_path)

    monkeypatch.setattr(relocate.os, "rename", rename)
    if surface == "tool":
        result = json.loads(await ArtifactRelocateTool().execute(
            artifact_id="moving", target_path=str(target), _thread_id="move-owner",
        ))
    else:
        seed_account("fixture-user", "relocation-token")
        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/artifacts/moving/relocate", params={"token": "relocation-token"},
                json={"thread_id": "move-owner", "target_path": str(target)},
            )
        assert response.status_code == 200
        result = response.json()
    assert result["artifact"]["id"] == "moving"
    assert result["artifact"]["path"] == str(target)
    assert result["artifact"]["type"] == "code"
    assert result["artifact"]["title"] == "Custom title"
    assert _snapshot(path) == (
        "Original", (str(target), "code", "Custom title"), [], 1,
    )
    assert not source.exists()
    assert target.read_text() == "preserved bytes"
    _writer_free(path)
