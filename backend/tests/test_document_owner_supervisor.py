"""The ownership payload reports the condition the ownership PUT refuses on.

The control must not offer the person as an owner when taking the document over
would be refused, so the load payload carries ``multi_person`` and the refusal
reads the same predicate (``document_supervisor.is_multi_person``). One test
covers both sides of each installation shape so the two cannot drift.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import OTHER_USER, OWNER_USER
from infra.auth import require_human_user

PERSON = {"id": OWNER_USER, "token_kind": "session"}
SECOND_PERSON = OTHER_USER


def _view(artifact_id: str):
    """The ownership payload as the document load builds it."""
    import infra.config as config
    from infra.database import get_db
    from registry.document_supervisor import is_multi_person, ownership
    from registry import models

    db = get_db()
    try:
        return ownership(
            db, models.get_artifact(artifact_id, db=db),
            user_id=PERSON["id"], workspace=config.WORKSPACE, multi_person=is_multi_person(db),
        )
    finally:
        db.close()


def _one_person_account():
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("DELETE FROM auth_tokens WHERE user_id=?", (SECOND_PERSON,))
        db.execute("DELETE FROM users WHERE id=?", (SECOND_PERSON,))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def owned_document(isolated_db):
    """A registered document owned by an agent thread, on a two-person installation."""
    import infra.config as config
    from infra.database import get_db
    from registry import service

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,agent_enabled,created_at,updated_at) "
            "VALUES ('agent-1',?,'Document agent',1,'t','t')",
            (OWNER_USER,),
        )
        db.commit()
        document = config.WORKSPACE / "shared" / "documents" / "reference.md"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text("# Reference\n\nBody.\n", encoding="utf-8")
        artifact = service.register_write_strict(
            "agent-1", str(document),
            metadata={"update_trigger": "when the source facts change"}, db=db,
        )
        db.commit()
    finally:
        db.close()

    from api.registry_ownership import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_human_user] = lambda: PERSON
    return TestClient(app), artifact["id"]


def test_multi_person_installation_reports_the_flag_and_refuses_the_person(owned_document):
    from registry.document_supervisor import MULTI_PERSON_REFUSAL

    client, artifact_id = owned_document

    assert _view(artifact_id)["multi_person"] is True
    refused = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == MULTI_PERSON_REFUSAL


def test_single_person_installation_reports_no_flag_and_allows_the_person(owned_document):
    client, artifact_id = owned_document
    _one_person_account()

    assert _view(artifact_id)["multi_person"] is False
    changed = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"})
    assert changed.status_code == 200, changed.text
    payload = changed.json()["ownership"]
    assert payload["multi_person"] is False
    assert payload["owner"]["kind"] == "person"


def _comment_recipients(artifact_id: str) -> tuple[str, ...]:
    """Who a person's new comment on the document is delivered to."""
    from doceditor import feedback_comments
    from infra.database import get_db

    db = get_db()
    try:
        comment: dict[str, object] = {"id": "c-new", "artifact_id": artifact_id, "author_kind": "user",
                   "author_thread_id": None, "parent_id": None, "tagged_agent_ids": []}
        return feedback_comments.recipients(db, comment)
    finally:
        db.close()


def _add_agent(thread_id: str, title: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,agent_enabled,created_at,updated_at) VALUES (?,?,?,1,'t','t')",
            (thread_id, PERSON["id"], title),
        )
        db.commit()
    finally:
        db.close()


def test_taking_a_document_over_routes_its_comments_to_the_supervisor(owned_document):
    """With no owning agent, the supervisor the header shows receives the comments."""
    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    assert _comment_recipients(artifact_id) == ("agent-1",)

    taken = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"})
    assert taken.status_code == 200, taken.text
    # The agent that owned it until now becomes its supervisor, and gets the person's comments.
    assert taken.json()["ownership"]["supervisor"]["id"] == "agent-1"
    assert _comment_recipients(artifact_id) == ("agent-1",)

    picked = client.put(
        f"/api/artifacts/{artifact_id}/ownership",
        json={"owner": "user", "supervisor_thread_id": "agent-2"},
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["ownership"]["supervisor"]["id"] == "agent-2"
    assert _comment_recipients(artifact_id) == ("agent-2",)


def test_a_document_the_person_owns_takes_agent_writes_as_suggestions(owned_document):
    """Once the person owns it, the agent that owned it can no longer edit it directly."""
    from infra.database import get_db
    from registry.shared_principals import UI_PRINCIPAL, artifact_owner, may_edit_document

    client, artifact_id = owned_document
    _one_person_account()
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"}).status_code == 200
    db = get_db()
    try:
        owner = artifact_owner(artifact_id, db=db)
        assert owner == UI_PRINCIPAL
        path_rel = "shared/documents/reference.md"
        decision = may_edit_document("agent-1", False, owner=owner, conductor=None,
                                     path_rel=path_rel, db=db, artifact_id=artifact_id)
        assert decision.basis == "refused"
        assert may_edit_document("x", True, owner=owner, conductor=None, path_rel=path_rel, db=db).basis == "owner"
    finally:
        db.close()


def test_a_picked_supervisor_on_an_agent_owned_document_receives_comments_alongside_its_owner(owned_document):
    """Comments go to the supervisor the header shows, and the owning agent, which can edit, keeps them too."""
    client, artifact_id = owned_document
    _add_agent("agent-2", "Research coordinator")
    assert _view(artifact_id)["supervisor"]["id"] == "agent-1"
    assert _comment_recipients(artifact_id) == ("agent-1",)
    picked = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"})
    assert picked.status_code == 200, picked.text
    view = picked.json()["ownership"]
    assert (view["owner"]["id"], view["supervisor"]["id"]) == ("agent-1", "agent-2")
    assert _comment_recipients(artifact_id) == ("agent-1", "agent-2")


def test_a_reopened_comment_reaches_the_same_agents_as_a_new_one(owned_document):
    """With a picked supervisor, a re-open goes to the supervisor and the owning agent, as a new comment does."""
    from doc_feedback_helpers import add_comment
    from doceditor import comments, feedback_batch_edits, feedback_batches
    from infra.database import get_db, write_transaction

    client, artifact_id = owned_document
    _add_agent("agent-2", "Research coordinator")
    _add_agent("author", "Reviewer")
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"}).status_code == 200
    db = get_db()
    try:
        comment, _ = add_comment(db, artifact_id)
        resolved = comments.update(comment["id"], {"resolved": True}, db=db)
        with write_transaction(db, scope="test_reopen_recipients") as connection:
            feedback_batch_edits.record_reopen(
                connection, resolved, user_id=PERSON["id"], user_display="Person",
                delivery_policy=feedback_batches.DOCUMENT_HELD,
            )
        reached = tuple(row[0] for row in db.execute(
            """SELECT batch.recipient_id FROM doc_feedback_batches batch
               JOIN doc_feedback_batch_items item ON item.batch_id=batch.id
               WHERE item.comment_id=? AND item.event='reopened' ORDER BY batch.recipient_id""",
            (comment["id"],),
        ).fetchall())
    finally:
        db.close()
    assert reached == _comment_recipients(artifact_id) == ("agent-1", "agent-2")


def test_an_owner_change_keeps_the_picked_supervisor(owned_document):
    """The server keeps a field the PUT leaves out, so an Owner pick never drops the person's Supervisor pick."""
    client, artifact_id = owned_document
    _add_agent("agent-2", "Research coordinator")
    _add_agent("agent-3", "Release planning")
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"}).status_code == 200

    moved = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "agent-3"})
    assert moved.status_code == 200, moved.text
    view = moved.json()["ownership"]
    assert (view["owner"]["id"], view["supervisor"]["id"], view["supervisor"]["source"]) == ("agent-3", "agent-2", "picked")


def _set_owner(artifact_id: str, owner: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("UPDATE artifact_registrations SET parent_thread_id=? WHERE artifact_id=?", (owner, artifact_id))
        db.commit()
    finally:
        db.close()


def test_a_ui_owner_reads_as_the_person_and_a_supervisor_pick_keeps_it(owned_document):
    """A ``ui`` owner reads as the person's, as write authority treats it; a Supervisor pick sends no owner."""
    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    _set_owner(artifact_id, "ui")
    assert _view(artifact_id)["owner"]["kind"] == "person"

    picked = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"})
    assert picked.status_code == 200, picked.text
    view = picked.json()["ownership"]
    assert view["owner"]["kind"] == "person"
    assert view["supervisor"]["id"] == "agent-2"
    assert _comment_recipients(artifact_id) == ("agent-2",)


def _batched_to(artifact_id: str, comment_id: str) -> set[str]:
    from infra.database import get_db

    db = get_db()
    try:
        return {str(row[0]) for row in db.execute(
            """SELECT batch.recipient_id FROM doc_feedback_batches batch
               JOIN doc_feedback_batch_items item ON item.batch_id=batch.id
               WHERE batch.artifact_id=? AND item.comment_id=?""",
            (artifact_id, comment_id),
        ).fetchall()}
    finally:
        db.close()


def test_an_agent_comment_on_the_persons_document_reaches_the_person_and_their_own_does_not(owned_document):
    """The owner is a recipient whatever its kind; the author is dropped in ``feedback_comments.recipients``."""
    from doc_feedback_helpers import add_comment
    from infra.database import get_db

    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    _add_agent("author", "Reviewer")
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"}).status_code == 200
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"}).status_code == 200
    db = get_db()
    try:
        from_agent, _ = add_comment(db, artifact_id)
        from_person, _ = add_comment(db, artifact_id, author_kind="user", author_id=None, author="Person")
    finally:
        db.close()
    assert _batched_to(artifact_id, from_agent["id"]) == {"agent-2", "ui"}
    assert _batched_to(artifact_id, from_person["id"]) == {"agent-2"}


def test_a_released_document_routes_as_its_header_reads_and_never_to_no_one(owned_document):
    """Released, the registry has no owner while the artifact row still names the former one.

    With a supervisor the header shows, comments go to it alone; with none, they still
    reach the agent that last owned the document rather than no one.
    """
    from infra.database import get_db
    from registry import models, ownership
    from registry.document_supervisor import SUPERVISOR_KEY
    from registry.identity_metadata import merge_identity_metadata
    from registry.shared_principals import artifact_owner

    _client, artifact_id = owned_document
    _add_agent("agent-2", "Research coordinator")
    db = get_db()
    try:
        identity = db.execute(
            "SELECT integration,type,external_id FROM artifact_identities WHERE id=?", (artifact_id,),
        ).fetchone()
        ownership.release_artifact(
            integration=identity[0], type=identity[1], external_id=identity[2], actor_thread_id="agent-1",
            authority="current-owner-handoff", reason="test", source_path="test", db=db,
        )
        db.commit()
        assert artifact_owner(artifact_id, db=db) is None
        assert models.get_artifact(artifact_id, db=db)["parent_thread_id"] == "agent-1"
    finally:
        db.close()
    assert _view(artifact_id)["owner"]["kind"] == "none"
    assert _comment_recipients(artifact_id) == ("agent-1",)

    db = get_db()
    try:
        merge_identity_metadata(db, artifact_id, {SUPERVISOR_KEY: "agent-2"})
        db.commit()
    finally:
        db.close()
    assert _view(artifact_id)["supervisor"]["id"] == "agent-2"
    assert _comment_recipients(artifact_id) == ("agent-2",)


@pytest.mark.parametrize("shared_owner", ["ui", "project:p1"])
def test_a_shared_document_is_not_changed_by_one_of_several_people(owned_document, shared_owner):
    """Everyone shares a ``ui`` or ``project:`` document, so with two people neither field changes."""
    from registry.document_supervisor import SHARED_REFUSAL

    client, artifact_id = owned_document
    _set_owner(artifact_id, shared_owner)
    assert _view(artifact_id)["can_change"] is False
    for change in ({"supervisor_thread_id": "agent-1"}, {"owner": "agent-1"}):
        refused = client.put(f"/api/artifacts/{artifact_id}/ownership", json=change)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == SHARED_REFUSAL
    view = _view(artifact_id)
    assert view["owner"]["kind"] == ("person" if shared_owner == "ui" else "none")
    assert (view["supervisor"] or {}).get("source") != "picked"


@pytest.mark.parametrize("shared_owner", ["ui", "project:p1"])
def test_one_persons_owner_pick_leaves_a_shared_document_reachable_by_the_other(owned_document, shared_owner):
    """One of two people tries to give a shared document their own agent; it stays shared, so the other still reaches it."""
    from infra.auth import may_access_artifact
    from infra.database import get_db
    from registry import models
    from registry.document_supervisor import SHARED_REFUSAL

    client, artifact_id = owned_document
    url = f"/api/artifacts/{artifact_id}/ownership"
    _set_owner(artifact_id, shared_owner)
    picked = client.put(url, json={"owner": "agent-1"})
    assert (picked.status_code, picked.json()["detail"]) == (409, SHARED_REFUSAL)
    db = get_db()
    try:
        assert may_access_artifact(models.get_artifact(artifact_id, db=db)["parent_thread_id"], SECOND_PERSON, db=db)
    finally:
        db.close()
    # The other person reaches the document (a missing one answers 404) and meets the same refusal.
    client.app.dependency_overrides[require_human_user] = lambda: {"id": SECOND_PERSON, "token_kind": "session"}
    other = client.put(url, json={"owner": "agent-1"})
    assert (other.status_code, other.json()["detail"]) == (409, SHARED_REFUSAL)


def test_another_persons_document_reads_as_missing(owned_document):
    """The ownership change refuses a document in another person's thread, as an unknown id."""
    from infra.database import get_db

    client, artifact_id = owned_document
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,agent_enabled,created_at,updated_at) "
            "VALUES ('their-agent',?,'Their agent',1,'t','t')",
            (SECOND_PERSON,),
        )
        db.commit()
    finally:
        db.close()
    _set_owner(artifact_id, "their-agent")

    refused = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "agent-1"})
    assert refused.status_code == 404, refused.text
    assert _view(artifact_id)["owner"]["id"] == "their-agent"
