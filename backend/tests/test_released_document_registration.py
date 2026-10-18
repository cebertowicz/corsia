from __future__ import annotations

import json

import pytest

from delegation_docs_fixtures import DOC_BODY, _OWNER, _mk_thread

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def registrable_workspace_root(integration_workspace, monkeypatch):
    from tools import sandbox_config

    monkeypatch.setattr(
        sandbox_config,
        "_STATIC_ALLOWED_RW",
        [integration_workspace.resolve()],
    )


def _thread_document(workspace, thread_id):
    path = workspace / ".threads" / thread_id / "working" / "released-note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DOC_BODY)
    return path


def _registration_state(db, artifact_id, thread_id):
    row = db.execute(
        """SELECT relationship_state, surfacing, retired_at
           FROM artifact_registrations
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact_id, thread_id),
    ).fetchone()
    return dict(row) if row else None


async def _register(path, thread_id):
    from tools.artifacts import ArtifactRegisterTool

    return await ArtifactRegisterTool().execute(path=str(path), _thread_id=thread_id)


@pytest.mark.asyncio
async def test_released_working_document_registration_keeps_it_ownerless(
    integration_client,
    integration_workspace,
):
    from infra.database import get_db
    from registry import models
    from tools.documents import DocCommentCreateTool

    former_owner = _mk_thread(
        integration_client, _OWNER, title="Released document owner"
    )
    commenter = _mk_thread(integration_client, _OWNER, title="Document commenter")
    path = _thread_document(integration_workspace, former_owner)
    registered = json.loads(await _register(path, former_owner))["artifact"]

    db = get_db()
    try:
        released = models.release_artifact(
            integration=registered["integration"],
            type=registered["type"],
            external_id=registered["external_id"],
            actor_thread_id=former_owner,
            authority="test",
            reason="simulate settled ephemeral working document",
            source_path="test_released_document_registration",
            db=db,
        )
        assert released["old_owner_thread_id"] == former_owner
        assert _registration_state(db, registered["id"], former_owner)[
            "relationship_state"
        ] == "subscriber-recent"
    finally:
        db.close()

    comment = json.loads(
        await DocCommentCreateTool().execute(
            doc_path=str(path),
            quote="A unique tail sentence",
            body="A comment on a released document.",
            _thread_id=commenter,
        )
    )["comment"]

    re_registered = json.loads(await _register(path, former_owner))["artifact"]

    assert comment["artifact_id"] == registered["id"]
    assert re_registered["id"] == registered["id"]

    db = get_db()
    try:
        saved_comment = db.execute(
            "SELECT artifact_id FROM doc_comments WHERE id=?", (comment["id"],)
        ).fetchone()
        assert saved_comment["artifact_id"] == registered["id"]
        assert _registration_state(db, registered["id"], commenter)[
            "relationship_state"
        ] == "subscriber-recent"
        assert _registration_state(db, registered["id"], former_owner)[
            "relationship_state"
        ] == "subscriber-recent"
        owner_rows = db.execute(
            """SELECT parent_thread_id FROM artifact_registrations
               WHERE artifact_id=?
                 AND relationship_state IN ('owner-active', 'owner-pinned')""",
            (registered["id"],),
        ).fetchall()
        assert owner_rows == []
    finally:
        db.close()
