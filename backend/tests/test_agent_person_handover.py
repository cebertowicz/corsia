"""Agent handover shares the person's document ownership rules."""

import json

import pytest

from tests.test_document_owner_supervisor import owned_document as owned_document, _one_person_account, _view


async def _handover(artifact_id, actor):
    from infra.database import get_db
    from registry import models
    from tools.artifact_ownership import ArtifactRehomeTool

    db = get_db()
    artifact = models.get_artifact(artifact_id, db=db)
    db.close()
    return await ArtifactRehomeTool().execute(
        integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
        new_owner_thread_id="user", reason="Give the person their document", _thread_id=actor,
    )


@pytest.mark.asyncio
async def test_owner_hands_document_to_person_with_former_owner_as_supervisor(owned_document):
    _, artifact_id = owned_document
    _one_person_account()
    result = json.loads(await _handover(artifact_id, "agent-1"))
    assert result["artifact"]
    assert result["event_id"]
    assert result["old_owner_thread_id"] == "agent-1"
    assert result["new_owner_thread_id"] == "ui"
    view = _view(artifact_id)
    assert view["owner"]["kind"] == "person"
    assert view["owner"]["label"] == "You"
    assert view["supervisor"]["id"] == "agent-1"


@pytest.mark.asyncio
async def test_owner_worker_can_handover_but_unrelated_agent_is_refused(owned_document):
    from infra.database import get_db
    from tests.test_document_owner_supervisor import PERSON

    _, artifact_id = owned_document
    _one_person_account()
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES ('worker',?,'Worker','agent-1','t','t')", (PERSON["id"],))
    db.execute("INSERT INTO threads (id,user_id,title,agent_enabled,created_at,updated_at) VALUES ('other',?,'Other',1,'t','t')", (PERSON["id"],))
    db.commit()
    db.close()
    refused = await _handover(artifact_id, "other")
    assert refused == "Error: Ask the document's owner or their worker to give it to the person."
    assert json.loads(await _handover(artifact_id, "worker"))["artifact"]
    assert _view(artifact_id)["supervisor"]["id"] == "agent-1"


@pytest.mark.asyncio
async def test_nested_worker_edit_and_handover_use_same_owner(owned_document):
    from infra.database import get_db
    from registry.shared_principals import may_edit_document
    from tests.test_document_owner_supervisor import PERSON

    _, artifact_id = owned_document
    _one_person_account()
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES ('worker-1',?,'Worker','agent-1','t','t')", (PERSON['id'],))
    db.execute("INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) VALUES ('worker-2',?,'Worker','worker-1','t','t')", (PERSON['id'],))
    db.commit()
    assert may_edit_document('worker-2', False, owner='agent-1', conductor=None, path_rel='doc', db=db).basis == 'owner_subagent'
    db.close()
    assert json.loads(await _handover(artifact_id, 'worker-2'))['artifact']
    db = get_db()
    assert may_edit_document('worker-2', False, owner='ui', conductor=None, path_rel='doc', db=db).basis == 'refused'
    db.close()


@pytest.mark.asyncio
async def test_handover_emits_ownership_events(owned_document, monkeypatch):
    from infra import product_events

    _, artifact_id = owned_document
    _one_person_account()
    events = []
    monkeypatch.setattr(product_events, 'emit_product_event',
                        lambda category, action, **kw: events.append((category, action, kw['properties'])))
    assert json.loads(await _handover(artifact_id, 'agent-1'))['artifact']
    assert events == [
        ('documents', 'owner_changed', {'from': 'agent', 'to': 'person'}),
        ('documents', 'supervisor_changed', {'from': 'none', 'to': 'agent'}),
    ]


@pytest.mark.asyncio
async def test_handover_succeeds_with_two_people(owned_document):
    _, artifact_id = owned_document
    result = json.loads(await _handover(artifact_id, "agent-1"))
    assert result["new_owner_thread_id"] == "ui"
    assert _view(artifact_id)["owner"]["label"] == "You"


from tests.test_shared_ownership_assignment import assignment_scope as assignment_scope, _register


PROJECT_REFUSAL = "Error: Project documents stay with the project's conductor; it edits them directly."


def _events(db, artifact_id):
    return db.execute("SELECT COUNT(*) FROM artifact_registration_events WHERE artifact_id=?", (artifact_id,)).fetchone()[0]


def _project_document(scope, *, owner):
    """A project instruction document that is released (``owner`` None) or owned by ``project:project-alpha``."""
    from registry import models

    path = scope["root"] / "context" / "instruction.md"
    path.parent.mkdir()
    path.write_text("# Instruction\n", encoding="utf-8")
    artifact_id = _register(scope, path, scope["owner"])
    artifact = models.get_artifact(artifact_id, db=scope["db"])
    if owner is None:
        models.release_artifact(
            integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
            actor_thread_id=scope["owner"], authority="current-owner-handoff", reason="release", source_path="test", db=scope["db"],
        )
    else:
        models.rehome_artifact(
            integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
            new_owner_thread_id=owner, actor_thread_id=None, authority="system",
            reason="test", source_path="test", db=scope["db"],
        )
    scope["db"].commit()
    return path, artifact_id


def _with_workers(scope):
    """The conductor's own worker and another project's conductor, to ask as well."""
    from tests.registry_helpers import make_session

    scope["db"].execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("project-other", "Other", "workspace://workspace-stable-id/projects/other", "active", "t", "t"),
    )
    other = make_session(scope["db"], project_id="project-other", title="Other conductor", prefix="other-conductor")
    scope["db"].execute("UPDATE projects SET conductor_thread_id=? WHERE id='project-other'", (other,))
    scope["db"].execute(
        "INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at) "
        "SELECT 'conductor-worker',user_id,'Worker',id,'t','t' FROM threads WHERE id=?",
        (scope["conductor"],),
    )
    scope["db"].commit()
    return [scope["conductor"], "conductor-worker", other]


async def _assert_refused_for_everyone(scope, artifact_id, path, owner_before, monkeypatch):
    """No agent hands a project document to the person: nothing changes and nothing is emitted."""
    from infra import product_events
    from registry import models
    from registry.shared_principals import artifact_owner

    emitted = []
    monkeypatch.setattr(product_events, "emit_product_event", lambda *a, **kw: emitted.append((a, kw)))
    body_before, events_before = path.read_text(encoding="utf-8"), _events(scope["db"], artifact_id)
    artifact_before = models.get_artifact(artifact_id, db=scope["db"])
    for actor in _with_workers(scope):
        assert await _handover(artifact_id, actor) == PROJECT_REFUSAL
    assert artifact_owner(artifact_id, db=scope["db"]) == owner_before
    assert models.get_artifact(artifact_id, db=scope["db"]) == artifact_before
    assert path.read_text(encoding="utf-8") == body_before
    assert _events(scope["db"], artifact_id) == events_before
    assert emitted == []


@pytest.mark.asyncio
async def test_no_agent_hands_a_released_project_document_to_person(assignment_scope, monkeypatch):
    scope = assignment_scope
    _one_person_account()
    path, artifact_id = _project_document(scope, owner=None)
    await _assert_refused_for_everyone(scope, artifact_id, path, None, monkeypatch)


@pytest.mark.asyncio
async def test_no_agent_hands_a_project_owned_document_to_person(assignment_scope, monkeypatch):
    scope = assignment_scope
    _one_person_account()
    path, artifact_id = _project_document(scope, owner="project:project-alpha")
    await _assert_refused_for_everyone(scope, artifact_id, path, "project:project-alpha", monkeypatch)


@pytest.mark.asyncio
async def test_project_conductor_registers_unowned_file_then_hands_it_to_person(assignment_scope):
    """A file the conductor registers is the conductor's own, an agent-owned document, so it may still be handed over."""
    from tools.artifacts import ArtifactRegisterTool

    scope = assignment_scope
    _one_person_account()
    path = scope["root"] / "context" / "instruction.md"
    path.parent.mkdir()
    path.write_text("# Instruction\n", encoding="utf-8")
    registered = json.loads(await ArtifactRegisterTool(workspace=scope["workspace"]).execute(
        path=str(path), _thread_id=scope["conductor"],
    ))["artifact"]
    result = json.loads(await _handover(registered["id"], scope["conductor"]))
    assert result["artifact"]
    assert _view(registered["id"])["owner"]["label"] == "You"


def test_person_still_takes_a_released_project_document_in_the_ui(assignment_scope):
    """Only the agent route refuses; the signed-in person's own take of a released project document is unchanged."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.registry_ownership import router
    from infra.auth import require_human_user
    from registry.shared_principals import artifact_owner
    from tests.test_document_owner_supervisor import PERSON

    scope = assignment_scope
    _one_person_account()
    _path, artifact_id = _project_document(scope, owner=None)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_human_user] = lambda: PERSON
    taken = TestClient(app).put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"})
    assert taken.status_code == 200, taken.text
    assert artifact_owner(artifact_id, db=scope["db"]) == "ui"
    assert _view(artifact_id)["owner"]["label"] == "You"
