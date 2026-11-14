"""Who the owner and supervisor pickers offer, and how a stored supervisor pick holds.

Split from ``test_document_owner_supervisor``, whose fixture and helpers these
tests share: the picker's scope (the document's own project or workspace), the
scope-lookup failure, the owner's word on every person-facing surface, the
events an ownership change emits, and the stored pick across archive, owner
changes and a move to a multi-person install.
"""

from __future__ import annotations

import pytest

from auth_fixtures import OTHER_TOKEN, OTHER_USER, seed_account
from test_document_owner_supervisor import (  # noqa: F401
    PERSON,
    _add_agent,
    _comment_recipients,
    _one_person_account,
    _set_owner,
    _view,
    owned_document,
)


def _project_document(slug: str = "acme") -> str:
    """A document inside project ``p1``'s folder (no conductor), registered by workspace agent ``agent-1``."""
    import infra.config as config
    from infra.database import get_db
    from registry import service

    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p1',?,'t','t')", (slug.title(),))
        db.execute(
            "INSERT INTO threads (id,user_id,title,agent_enabled,project_id,created_at,updated_at) "
            "VALUES ('proj-agent',?,'Project agent',1,'p1','t','t')", (PERSON["id"],),
        )
        document = config.WORKSPACE / "projects" / slug / "shared" / "documents" / "plan.md"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text("# Plan\n", encoding="utf-8")
        artifact = service.register_write_strict(
            "agent-1", str(document), metadata={"update_trigger": "when the plan changes"}, db=db,
        )
        db.commit()
        return artifact["id"]
    finally:
        db.close()


def test_the_picker_lists_the_documents_own_project_agents_when_the_project_has_no_conductor(owned_document):
    """The picker's scope is the document's, not the owner's or the conductor's."""
    _client, workspace_document = owned_document
    _one_person_account()
    artifact_id = _project_document()
    _set_owner(artifact_id, "ui")

    view = _view(artifact_id)
    assert [choice["id"] for choice in view["choices"]] == ["proj-agent"]
    assert view["scope"] == "project"
    assert _view(workspace_document)["scope"] == "workspace"
    assert [choice["id"] for choice in _view(workspace_document)["choices"]] == ["agent-1"]


def test_the_picker_choices_are_the_agents_an_explicit_owner_resolves_to(owned_document):
    """One query decides who an owner can be: every choice resolves, and a thread that is no agent does not."""
    from infra.database import get_db
    from registry.shared_principals import resolve_explicit_owner

    _client, artifact_id = owned_document
    _add_agent("agent-2", "Research coordinator")
    _add_agent("agent-3", "Release planning")
    _archive_agent("agent-3")
    db = get_db()
    try:
        db.execute("INSERT INTO threads (id,user_id,title,agent_enabled,created_at,updated_at) VALUES ('plain',?,'Not an agent',0,'t','t')", (PERSON["id"],))
        db.commit()
        choices = [choice["id"] for choice in _view(artifact_id)["choices"]]
        assert choices == ["agent-1", "agent-2"]
        assert [resolve_explicit_owner(choice, project_id=None, user_id=PERSON["id"], db=db) for choice in choices] == choices
        # Neither a thread that is no agent nor an archived agent can be picked or resolved.
        for refused in ("plain", "agent-3"):
            with pytest.raises(ValueError):
                resolve_explicit_owner(refused, project_id=None, user_id=PERSON["id"], db=db)
    finally:
        db.close()


def test_a_scope_lookup_failure_is_logged_and_offers_nothing_the_put_would_not_honor(owned_document, monkeypatch, caplog):
    import registry.document_supervisor as supervisor
    from api.registry_ownership import AGENT_CHOICE_REFUSAL

    client, artifact_id = owned_document

    def boom(*_args, **_kwargs):
        raise OSError("scope unreadable")

    monkeypatch.setattr(supervisor, "document_project", boom)
    with caplog.at_level("WARNING", logger="registry.document_supervisor"):
        view = _view(artifact_id)
    # No choices, and no scope to name, so the picker can't claim the scope has no agents.
    assert (view["choices"], view["scope"]) == ([], None)
    assert any("resolve the scope" in record.getMessage() and record.exc_info for record in caplog.records)
    refused = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-1"})
    assert (refused.status_code, refused.json()["detail"]) == (400, AGENT_CHOICE_REFUSAL)


def test_the_owner_reads_the_same_word_on_every_person_facing_surface(owned_document):
    from infra.database import get_db
    from registry import models
    from registry.doc_freshness import artifact_freshness
    from registry.shared_principals import person_owner_label

    _client, artifact_id = owned_document
    _one_person_account()
    db = get_db()
    try:
        assert person_owner_label("ui", db=db, multi_person=False) == "You"
        assert person_owner_label("project:p1", db=db, multi_person=False) == "Not set"
        assert person_owner_label(None, db=db, multi_person=False) == "Not set"
        assert person_owner_label("agent-1", db=db, multi_person=False) == "Document agent"
        # While more than one person uses CORSIA, no person owns a shared document: no agent owns it yet.
        assert person_owner_label("ui", db=db, multi_person=True) == "Not set"
        assert person_owner_label("project:p1", db=db, multi_person=True) == "Not set"
        assert person_owner_label(None, db=db, multi_person=True) == "Not set"
        assert person_owner_label("agent-1", db=db, multi_person=True) == "Document agent"
    finally:
        db.close()
    assert _view(artifact_id)["owner"]["label"] == "Document agent"
    _set_owner(artifact_id, "ui")
    assert _view(artifact_id)["owner"]["label"] == "You"
    db = get_db()
    try:
        artifact = models.get_artifact(artifact_id, db=db)
        path = artifact["path"] or artifact["external_id"]
        # Agents keep their own wording; every person-facing read asks for the person's.
        assert artifact_freshness(artifact, path, db=db)["owner_name"] == "the user"
        assert artifact_freshness(artifact, path, db=db, multi_person=False)["owner_name"] == "You"
        assert artifact_freshness(artifact, path, db=db, multi_person=True)["owner_name"] == "Not set"
    finally:
        db.close()
    seed_account(OTHER_USER, OTHER_TOKEN)
    assert _view(artifact_id)["owner"]["label"] == "Not set"
    _one_person_account()
    _set_owner(artifact_id, "project:p1")
    assert _view(artifact_id)["owner"]["label"] == "Not set"


def test_each_owner_or_supervisor_change_emits_one_event_and_a_no_op_none(owned_document, monkeypatch):
    import api.registry_ownership as ownership_api

    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        ownership_api, "emit_product_event",
        lambda category, action, **kw: events.append((category, action, kw["properties"])),
    )
    url = f"/api/artifacts/{artifact_id}/ownership"

    assert client.put(url, json={"owner": "agent-2"}).status_code == 200
    assert events == [("documents", "owner_changed", {"from": "agent", "to": "agent"})]
    events.clear()
    assert client.put(url, json={"supervisor_thread_id": "agent-1"}).status_code == 200
    assert events == [("documents", "supervisor_changed", {"from": "none", "to": "agent"})]
    events.clear()
    assert client.put(url, json={"owner": "agent-2", "supervisor_thread_id": "agent-1"}).status_code == 200
    assert events == []


def test_a_rejected_pick_says_who_can_be_picked(owned_document):
    from api.registry_ownership import AGENT_CHOICE_REFUSAL, PERSON_OR_AGENT_CHOICE_REFUSAL

    client, artifact_id = owned_document
    url = f"/api/artifacts/{artifact_id}/ownership"
    supervisor = client.put(url, json={"supervisor_thread_id": "nobody"})
    assert (supervisor.status_code, supervisor.json()["detail"]) == (400, AGENT_CHOICE_REFUSAL)
    # On a two-person install the person is not a choice, so the sentence doesn't offer them.
    multi = client.put(url, json={"owner": "nobody"})
    assert (multi.status_code, multi.json()["detail"]) == (400, AGENT_CHOICE_REFUSAL)
    _one_person_account()
    single = client.put(url, json={"owner": "nobody"})
    assert (single.status_code, single.json()["detail"]) == (400, PERSON_OR_AGENT_CHOICE_REFUSAL)


def test_the_refusal_sentences_reach_the_screen_in_the_payload(owned_document):
    from registry.document_supervisor import MULTI_PERSON_REFUSAL, SHARED_REFUSAL

    _client, artifact_id = owned_document
    view = _view(artifact_id)
    assert (view["refusal"], view["multi_person_refusal"]) == (None, MULTI_PERSON_REFUSAL)
    _set_owner(artifact_id, "ui")
    assert _view(artifact_id)["refusal"] == SHARED_REFUSAL
    _one_person_account()
    assert _view(artifact_id)["multi_person_refusal"] is None


def test_the_ownership_change_on_a_released_document_is_a_clear_refusal_not_a_404(owned_document):
    from infra.database import get_db
    from registry import ownership
    from registry.document_supervisor import RELEASED_REFUSAL

    client, artifact_id = owned_document
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
    finally:
        db.close()
    refused = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "agent-1"})
    assert (refused.status_code, refused.json()["detail"]) == (409, RELEASED_REFUSAL)
    # With no owner there is nothing to change, so the sentence speaks of picking one.
    assert RELEASED_REFUSAL == "No one owns this document, so an owner or supervisor can’t be picked. Ask CORSIA to give it an owner."
    # The picker says so before a pick and lists no one, on a multi-person install and on a single-person one.
    view = _view(artifact_id)
    assert (view["can_change"], view["refusal"], view["choices"]) == (False, RELEASED_REFUSAL, [])
    _one_person_account()
    view = _view(artifact_id)
    assert (view["can_change"], view["refusal"], view["choices"]) == (False, RELEASED_REFUSAL, [])


def test_an_archived_document_with_an_owner_reads_as_missing_not_as_owned_by_no_one(owned_document):
    from api.registry_ownership import NOT_FOUND_REFUSAL
    from infra.database import get_db

    client, artifact_id = owned_document
    db = get_db()
    try:
        db.execute("UPDATE artifact_identities SET state='archived' WHERE id=?", (artifact_id,))
        db.commit()
    finally:
        db.close()
    refused = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "agent-1"})
    assert (refused.status_code, refused.json()["detail"]) == (404, NOT_FOUND_REFUSAL)


def _stored_pick(artifact_id: str):
    from infra.database import get_db
    from registry import models
    from registry.document_supervisor import SUPERVISOR_KEY
    from registry.identity_metadata import parse_metadata

    db = get_db()
    try:
        return parse_metadata(models.get_artifact(artifact_id, db=db).get("metadata")).get(SUPERVISOR_KEY)
    finally:
        db.close()


def _pick_directly(artifact_id: str, thread_id: str) -> None:
    from infra.database import get_db
    from registry.document_supervisor import SUPERVISOR_KEY
    from registry.identity_metadata import merge_identity_metadata

    db = get_db()
    try:
        merge_identity_metadata(db, artifact_id, {SUPERVISOR_KEY: thread_id})
        db.commit()
    finally:
        db.close()


def _move_agent_to_project(thread_id: str, project_id: str | None) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        if project_id is not None:
            db.execute("INSERT OR IGNORE INTO projects (id,name,created_at,updated_at) VALUES (?, 'Other','t','t')", (project_id,))
        db.execute("UPDATE threads SET project_id=? WHERE id=?", (project_id, thread_id))
        db.commit()
    finally:
        db.close()


def _archive_agent(thread_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id=?", (thread_id,))
        db.commit()
    finally:
        db.close()


def _recording_events(monkeypatch) -> list[tuple[str, dict]]:
    import api.registry_ownership as ownership_api

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ownership_api, "emit_product_event", lambda category, action, **kw: events.append((action, kw["properties"])),
    )
    return events


def test_the_stored_pick_is_what_the_header_shows_and_comments_reach_until_its_agent_is_archived(owned_document):
    """Stored, shown and routed are one value: a pick is not re-checked on read, only its agent's availability."""
    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"}).status_code == 200

    # A known trade-off: a pick survives its agent moving out of the document's scope.
    _move_agent_to_project("agent-2", "p9")
    assert (_view(artifact_id)["supervisor"]["id"], _view(artifact_id)["supervisor"]["source"]) == ("agent-2", "picked")
    assert _comment_recipients(artifact_id) == ("agent-1", "agent-2")

    _archive_agent("agent-2")
    assert (_view(artifact_id)["supervisor"]["id"], _view(artifact_id)["supervisor"]["source"]) == ("agent-1", "owner")
    assert _comment_recipients(artifact_id) == ("agent-1",)
    assert _stored_pick(artifact_id) == "agent-2"


def test_an_explicit_choice_is_written_over_a_stored_pick_the_header_does_not_show(owned_document, monkeypatch):
    """Picking the owner, or clearing, replaces a stored pick whose agent is archived, so it can't come back over the choice."""
    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    _pick_directly(artifact_id, "agent-2")
    _archive_agent("agent-2")
    events = _recording_events(monkeypatch)

    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-1"}).status_code == 200
    assert _stored_pick(artifact_id) is None
    assert events == [("supervisor_changed", {"from": "agent", "to": "none"})]

    _pick_directly(artifact_id, "agent-2")
    events.clear()
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": None}).status_code == 200
    assert _stored_pick(artifact_id) is None
    assert events == [("supervisor_changed", {"from": "agent", "to": "none"})]


@pytest.mark.parametrize(("stored", "supervises", "actions"), [
    (None, "agent-1", ["owner_changed", "supervisor_changed"]),
    ("agent-2", "agent-2", ["owner_changed"]),
])
def test_a_person_taking_over_keeps_the_stored_pick_and_otherwise_the_previous_owner_supervises(
    owned_document, monkeypatch, stored, supervises, actions,
):
    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    if stored:
        _pick_directly(artifact_id, stored)
    events = _recording_events(monkeypatch)

    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"}).status_code == 200
    assert (_stored_pick(artifact_id), _view(artifact_id)["supervisor"]["id"]) == (supervises, supervises)
    assert [action for action, _ in events] == actions


def test_a_put_that_changes_nothing_stored_emits_nothing(owned_document, monkeypatch):
    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    _pick_directly(artifact_id, "agent-2")
    _archive_agent("agent-2")
    events = _recording_events(monkeypatch)

    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "agent-1"}).status_code == 200
    assert events == []
    assert _stored_pick(artifact_id) == "agent-2"


def test_a_pick_on_a_shared_document_keeps_routing_when_the_install_becomes_multi_person(owned_document):
    """A known trade-off: the earlier single-person pick stays, shown and routed, and no one may change it."""
    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Research coordinator")
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"}).status_code == 200
    assert client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"}).status_code == 200
    assert _comment_recipients(artifact_id) == ("agent-2",)

    seed_account(OTHER_USER, OTHER_TOKEN)
    view = _view(artifact_id)
    assert (view["supervisor"]["id"], view["can_change"], view["choices"], view["owner"]["label"]) == ("agent-2", False, [], "Not set")
    assert _comment_recipients(artifact_id) == ("agent-2",)
