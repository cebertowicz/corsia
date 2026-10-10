"""Description edits preserve the named caller's transaction and failure scope."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from infra import database
from registry import models
from tests.registry_helpers import seed_artifact


def _seed(db):
    seed_artifact(
        db, artifact_id="description-artifact", integration="test", type="document",
        external_id="description-item", parent_thread_id="description-owner",
        title="Original title", description="Original description",
    )
    db.commit()


def _stored(path):
    with closing(sqlite3.connect(path, timeout=0.05)) as observer:
        return observer.execute(
            "SELECT title,description FROM artifact_identities "
            "WHERE id='description-artifact'"
        ).fetchone()


def _assert_writer_available(path):
    with closing(sqlite3.connect(path, timeout=0.05)) as observer:
        observer.execute("BEGIN IMMEDIATE")
        observer.rollback()


@pytest.mark.parametrize("named", [False, True])
def test_description_edit_preserves_projection_and_commits_owned_edit(isolated_db, named):
    db = database.get_db()
    _seed(db)

    result = models.update_description(
        "description-artifact", "Updated description", db=db if named else None,
    )

    assert result["id"] == "description-artifact"
    assert result["title"] == "Original title"
    assert result["description"] == "Updated description"
    assert result["parent_thread_id"] == "description-owner"
    assert _stored(isolated_db) == ("Original title", "Updated description")
    assert not db.in_transaction
    _assert_writer_available(isolated_db)


def test_description_edit_does_not_commit_named_outer_work(isolated_db):
    """An outer rollback must undo both caller work and the description edit."""
    db = database.get_db()
    _seed(db)

    with pytest.raises(RuntimeError, match="abort description caller"):
        with database.write_transaction(db, scope="description_caller"):
            db.execute(
                "UPDATE artifact_identities SET title='Caller title' "
                "WHERE id='description-artifact'"
            )
            result = models.update_description(
                "description-artifact", "Uncommitted description", db=db,
            )
            assert result["description"] == "Uncommitted description"
            assert _stored(isolated_db) == ("Original title", "Original description")
            raise RuntimeError("abort description caller")

    assert _stored(isolated_db) == ("Original title", "Original description")
    assert not db.in_transaction
    _assert_writer_available(isolated_db)


def test_description_edit_refuses_implicit_foreign_work(isolated_db):
    """An anonymous pooled lookup does not authorize committing another writer."""
    db = database.get_db()
    _seed(db)
    db.execute(
        "UPDATE artifact_identities SET title='Caller title' "
        "WHERE id='description-artifact'"
    )
    try:
        with pytest.raises(database.TransactionOwnershipError):
            models.update_description("description-artifact", "Unauthorized edit")
        assert db.in_transaction
        assert _stored(isolated_db) == ("Original title", "Original description")
        assert db.execute(
            "SELECT title,description FROM artifact_identities "
            "WHERE id='description-artifact'"
        ).fetchone()["title"] == "Caller title"
    finally:
        db.rollback()


def test_missing_description_target_releases_named_writer(isolated_db):
    """A no-match UPDATE must not leave a clean supplied connection writing."""
    db = database.get_db()
    _seed(db)

    assert models.update_description("missing", "No edit", db=db) is None

    assert not db.in_transaction
    assert _stored(isolated_db) == ("Original title", "Original description")
    _assert_writer_available(isolated_db)


def test_rejected_description_rolls_back_only_its_own_write(isolated_db):
    """SQLite RAISE(FAIL) leaves changes pending unless the leaf rolls back."""
    db = database.get_db()
    _seed(db)
    db.execute(
        "CREATE TRIGGER reject_description AFTER UPDATE OF description "
        "ON artifact_identities BEGIN SELECT RAISE(FAIL,'description rejected'); END"
    )
    db.commit()
    with database.write_transaction(db, scope="description_failure_caller"):
        db.execute(
            "UPDATE artifact_identities SET title='Caller title' "
            "WHERE id='description-artifact'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="description rejected"):
            models.update_description("description-artifact", "Rejected edit", db=db)
        assert db.in_transaction
        assert db.execute(
            "SELECT description FROM artifact_identities WHERE id='description-artifact'"
        ).fetchone()[0] == "Original description"

    assert _stored(isolated_db) == ("Caller title", "Original description")
    _assert_writer_available(isolated_db)


class _CommitFailure(sqlite3.Connection):
    fail_commit = False

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("description commit rejected")
        super().commit()


def test_failed_description_commit_rolls_back_and_releases_writer(isolated_db):
    """Failure must propagate without leaving a lock or a later-committable edit."""
    with closing(sqlite3.connect(isolated_db, factory=_CommitFailure)) as db:
        db.row_factory = sqlite3.Row
        _seed(db)
        db.fail_commit = True

        with pytest.raises(sqlite3.OperationalError, match="description commit rejected"):
            models.update_description("description-artifact", "Failed edit", db=db)

        assert not db.in_transaction
        assert _stored(isolated_db) == ("Original title", "Original description")
        _assert_writer_available(isolated_db)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["http", "tool"])
async def test_description_callers_preserve_authorization_and_durable_result(
    integration_client, surface,
):
    """Both actual entry points persist the owner's edit and reject a foreign one."""
    from delegation_docs_fixtures import _OTHER, _OWNER, _mk_thread
    from tools.artifacts import ArtifactUpdateTool

    owner = _mk_thread(integration_client, _OWNER)
    foreign = _mk_thread(integration_client, _OTHER)
    artifact_id = f"surface-description-{surface}"
    db = database.get_db()
    seed_artifact(
        db, artifact_id=artifact_id, integration="test", type="document",
        external_id=f"surface-item-{surface}", parent_thread_id=owner,
        title="Surface document", description="Original description",
    )
    db.commit()

    if surface == "http":
        response = integration_client.patch(
            f"/api/artifacts/{artifact_id}?token={_OWNER}",
            json={"description": "Owner edit"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        denied = integration_client.patch(
            f"/api/artifacts/{artifact_id}?token={_OTHER}",
            json={"description": "Foreign edit"},
        )
        assert denied.status_code == 404
    else:
        result = json.loads(await ArtifactUpdateTool().execute(
            artifact_id=artifact_id, description="Owner edit", _thread_id=owner,
        ))
        denied = await ArtifactUpdateTool().execute(
            artifact_id=artifact_id, description="Foreign edit", _thread_id=foreign,
        )
        assert denied == "Error: Artifact not found"

    assert result["artifact"]["description"] == "Owner edit"
    assert result["artifact"]["parent_thread_id"] == owner
    with closing(sqlite3.connect(database.DB_PATH, timeout=0.05)) as observer:
        assert observer.execute(
            "SELECT description FROM artifact_identities WHERE id=?", (artifact_id,),
        ).fetchone()[0] == "Owner edit"
        observer.execute("BEGIN IMMEDIATE")
        observer.rollback()
