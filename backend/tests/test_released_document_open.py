"""A released document is still an active document: it opens, reads and takes comments.

Release removes the owner, not the document. Its state stays the identity's own
state; who owns it comes only from the registration. Writes that need an owner
(the owner and supervisor pick, marking it verified) stay refused until an agent
with authority gives it one. An archived or tombstoned identity stays refused
everywhere.
"""
from __future__ import annotations

import json
import uuid

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _post

pytestmark = pytest.mark.integration

_AUTH = "?token=" + _OWNER
_BODY = "# Released\n\nA unique tail sentence anchors here.\n"


@pytest.fixture(autouse=True)
def registrable_workspace_root(integration_workspace, monkeypatch):
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [integration_workspace.resolve()])


def _shared_document(client, workspace):
    """A shared document an agent registered, then released."""
    from infra.database import get_db
    from registry import models, service

    owner = _mk_thread(client, _OWNER, title="Former owner")
    path = workspace / "shared" / "documents" / f"released-{uuid.uuid4().hex[:8]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BODY, encoding="utf-8")
    db = get_db()
    try:
        artifact = service.register_write_strict(
            owner, str(path), metadata={"update_trigger": "when the facts change"}, db=db,
        )
        db.commit()
        return owner, path, artifact
    finally:
        db.close()


def _release(artifact, owner):
    from infra.database import get_db
    from registry import models

    db = get_db()
    try:
        models.release_artifact(
            integration=artifact["integration"], type=artifact["type"],
            external_id=artifact["external_id"], actor_thread_id=owner,
            authority="current-owner-handoff", reason="test release",
            source_path="test_released_document_open", db=db,
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def released(integration_client, integration_workspace):
    owner, path, artifact = _shared_document(integration_client, integration_workspace)
    _release(artifact, owner)
    return owner, path, artifact


def _retire(artifact_id, tombstone):
    from infra.database import get_db
    from registry import models

    db = get_db()
    try:
        models.retire(artifact_id, tombstone=tombstone, db=db)
        db.commit()
    finally:
        db.close()


def _revision(client, path, artifact_id):
    r = client.get(f"/api/files/revision{_AUTH}&path={path}&artifact_id={artifact_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _content(client, path, thread_id):
    r = client.get(f"/api/files/content{_AUTH}&path={path}&thread_id={thread_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _with_db(fn):
    from infra.database import get_db

    db = get_db()
    try:
        return fn(db)
    finally:
        db.close()


async def _tool_comment(artifact_id, thread_id):
    from tools.documents import DocCommentCreateTool

    return await DocCommentCreateTool().execute(
        artifact_id=artifact_id, quote="A unique tail sentence",
        body="A comment by id.", _thread_id=thread_id,
    )


def _user_id(thread_id):
    return _with_db(lambda db: db.execute(
        "SELECT user_id FROM threads WHERE id=?", (thread_id,),
    ).fetchone()["user_id"])


def _review_actor(thread_id):
    from doceditor.review_requests import ReviewActor

    return ReviewActor("agent", thread_id, thread_id, _user_id(thread_id), "Requester")


def test_a_released_document_reads_as_active_and_unowned(released):
    from registry import models
    from registry.shared_principals import artifact_owner

    owner, _path, artifact = released
    row = _with_db(lambda db: models.get_artifact(artifact["id"], db=db))
    assert (row["state"], row["parent_thread_id"], row["relationship_state"]) == (
        "active", owner, "subscriber-recent",
    )
    assert _with_db(lambda db: artifact_owner(artifact["id"], db=db)) is None


def test_its_open_tab_finds_it_and_the_page_shows_no_owner(integration_client, released):
    from registry.document_supervisor import RELEASED_REFUSAL

    _owner, path, artifact = released
    probe = _revision(integration_client, path, artifact["id"])
    assert (probe["exists"], probe["artifact_id"]) == (True, artifact["id"])

    viewer = _mk_thread(integration_client, _OWNER, title="Viewer")
    page = _content(integration_client, path, viewer)
    assert page["text"] == _BODY
    assert page["artifact"]["id"] == artifact["id"]
    ownership = page["ownership"]
    assert (ownership["owner"]["kind"], ownership["can_change"], ownership["refusal"]) == (
        "none", False, RELEASED_REFUSAL,
    )


def test_the_owner_pick_stays_a_clear_refusal(integration_client, released):
    from infra.auth import require_human_user
    from registry.document_supervisor import RELEASED_REFUSAL

    owner, _path, artifact = released
    person = {"id": _user_id(owner), "token_kind": "session"}
    overrides = integration_client.app.dependency_overrides
    overrides[require_human_user] = lambda: person
    try:
        refused = integration_client.put(
            f"/api/artifacts/{artifact['id']}/ownership", json={"owner": owner},
        )
    finally:
        overrides.pop(require_human_user, None)
    assert (refused.status_code, refused.json()["detail"]) == (409, RELEASED_REFUSAL)


@pytest.mark.asyncio
async def test_corsia_gives_it_an_owner_and_the_page_shows_that_owner(integration_client, released):
    """The refusal says to ask CORSIA; the CORSIA agent's rehome is the route that answers it."""
    from infra.corsia_agent import set_corsia_agent
    from registry import models
    from tools.artifact_ownership import ArtifactRehomeTool

    _owner, path, artifact = released
    corsia = _mk_thread(integration_client, _OWNER, title="CORSIA")
    target = _mk_thread(integration_client, _OWNER, title="New owner")
    other = _mk_thread(integration_client, _OWNER, title="Not CORSIA")
    _with_db(lambda db: (set_corsia_agent(corsia, db=db), db.commit()))

    def _rehome(actor):
        return ArtifactRehomeTool().execute(
            integration=artifact["integration"], type=artifact["type"],
            external_id=artifact["external_id"], new_owner_thread_id=target,
            reason="give the released document an owner", authority="workspace-corsia",
            _thread_id=actor,
        )

    denied = await _rehome(other)
    assert denied.startswith("Error: Re-home authority denied"), denied
    rehomed = await _rehome(corsia)
    assert not rehomed.startswith("Error"), rehomed

    row = _with_db(lambda db: models.get_artifact(artifact["id"], db=db))
    assert (row["state"], row["parent_thread_id"], row["relationship_state"]) == (
        "active", target, "owner-active",
    )
    ownership = _content(integration_client, path, target)["ownership"]
    assert (ownership["owner"]["kind"], ownership["owner"]["id"]) == ("agent", target)


@pytest.mark.asyncio
async def test_comments_by_id_reach_it_on_both_routes(integration_client, released):
    from doceditor.presence import observe_document_activity

    _owner, _path, artifact = released
    commenter = _mk_thread(integration_client, _OWNER, title="Commenter")

    by_route = _post(
        integration_client, _OWNER, artifact_id=artifact["id"],
        quote="A unique tail sentence", body="A comment by id.", as_session_id=commenter,
    )
    assert by_route.status_code == 200, by_route.text
    assert by_route.json()["comment"]["artifact_id"] == artifact["id"]

    result = await _tool_comment(artifact["id"], commenter)
    comment = json.loads(result)["comment"]
    assert comment["artifact_id"] == artifact["id"]

    # The owner's page shows the agent at work in the document.
    activity = _with_db(lambda db: observe_document_activity(
        commenter, "doc_comment_create", {"artifact_id": artifact["id"]}, result, False, db,
    ))
    assert activity is not None
    assert activity.frame["artifact_id"] == artifact["id"]


def test_it_takes_a_review_request_and_shows_where_the_agent_is_reading(integration_client, released):
    from doceditor import review_requests
    from infra.document_viewing import resolve_viewing_source

    _owner, path, artifact = released
    requester = _mk_thread(integration_client, _OWNER, title="Requester")

    found = _with_db(lambda db: review_requests._artifact(db, artifact["id"], _review_actor(requester)))
    assert found["id"] == artifact["id"]
    source = _with_db(lambda db: resolve_viewing_source(db, requester, {"artifact_id": artifact["id"]}))
    assert source is not None and source.path == str(path)


def test_marking_it_verified_waits_for_an_owner(released):
    from registry.doc_freshness import mark_verified

    _owner, _path, artifact = released
    with pytest.raises(ValueError, match="no registry owner"):
        _with_db(lambda db: mark_verified(artifact["id"], "ui", db=db))


@pytest.mark.parametrize("was_released", [False, True])
@pytest.mark.parametrize("tombstone", [False, True])
@pytest.mark.asyncio
async def test_an_archived_or_tombstoned_document_stays_refused_everywhere(
    integration_client, integration_workspace, was_released, tombstone,
):
    from doceditor import review_requests
    from doceditor.presence import observe_document_activity
    from infra.document_viewing import resolve_viewing_source
    from registry.doc_freshness import mark_verified

    owner, path, artifact = _shared_document(integration_client, integration_workspace)
    commenter = _mk_thread(integration_client, _OWNER, title="Commenter")
    comment = await _tool_comment(artifact["id"], commenter)
    assert not comment.startswith("Error"), comment
    if was_released:
        _release(artifact, owner)
    _retire(artifact["id"], tombstone)

    assert _revision(integration_client, path, artifact["id"])["exists"] is False
    by_route = _post(
        integration_client, _OWNER, artifact_id=artifact["id"],
        quote="A unique tail sentence", body="Too late.", as_session_id=commenter,
    )
    assert by_route.status_code == 422, by_route.text
    assert "does not resolve to an active artifact" in await _tool_comment(artifact["id"], commenter)
    with pytest.raises(PermissionError):
        _with_db(lambda db: review_requests._artifact(db, artifact["id"], _review_actor(commenter)))
    assert _with_db(lambda db: observe_document_activity(
        commenter, "doc_comment_create", {"artifact_id": artifact["id"]}, comment, False, db,
    )) is None
    assert _with_db(lambda db: resolve_viewing_source(db, commenter, {"artifact_id": artifact["id"]})) is None
    with pytest.raises(ValueError, match="artifact not found"):
        _with_db(lambda db: mark_verified(artifact["id"], "ui", db=db))
