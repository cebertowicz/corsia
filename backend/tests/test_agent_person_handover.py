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
    assert refused == "Error: Ask the document's owner or their worker to give it to the person; for an unowned project document, ask its conductor."
    assert json.loads(await _handover(artifact_id, "worker"))["artifact"]
    assert _view(artifact_id)["supervisor"]["id"] == "agent-1"


@pytest.mark.asyncio
async def test_handover_preserves_existing_multi_person_refusal(owned_document):
    from registry.document_supervisor import MULTI_PERSON_REFUSAL

    _, artifact_id = owned_document
    assert await _handover(artifact_id, "agent-1") == f"Error: {MULTI_PERSON_REFUSAL}"


from tests.test_shared_ownership_assignment import assignment_scope as assignment_scope, _register


@pytest.mark.asyncio
async def test_project_conductor_hands_released_document_to_person(assignment_scope):
    from registry import models
    from registry.shared_principals import artifact_owner

    scope = assignment_scope
    _one_person_account()
    path = scope["root"] / "context" / "instruction.md"
    path.parent.mkdir()
    path.write_text("# Instruction\n", encoding="utf-8")
    artifact_id = _register(scope, path, scope["owner"])
    artifact = models.get_artifact(artifact_id, db=scope["db"])
    models.release_artifact(
        integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
        actor_thread_id=scope["owner"], authority="current-owner-handoff", reason="release", source_path="test", db=scope["db"],
    )
    scope["db"].commit()
    assert artifact_owner(artifact_id, db=scope["db"]) is None
    result = json.loads(await _handover(artifact_id, scope["conductor"]))
    assert result["artifact"]
    assert _view(artifact_id)["owner"]["label"] == "You"
    event = scope["db"].execute(
        "SELECT actor_thread_id, authority FROM artifact_registration_events WHERE artifact_id=? AND event_type='ownership_rehomed' ORDER BY rowid DESC LIMIT 1", (artifact_id,),
    ).fetchone()
    assert tuple(event) == (scope["conductor"], "agent-handover-to-person")


@pytest.mark.asyncio
async def test_project_conductor_hands_project_sentinel_document_to_person(assignment_scope):
    from registry import models

    scope = assignment_scope
    _one_person_account()
    path = scope["root"] / "context" / "instruction.md"
    path.parent.mkdir()
    path.write_text("# Instruction\n", encoding="utf-8")
    artifact_id = _register(scope, path, scope["owner"])
    artifact = models.get_artifact(artifact_id, db=scope["db"])
    models.rehome_artifact(
        integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
        new_owner_thread_id="project:project-alpha", actor_thread_id=None, authority="system",
        reason="test", source_path="test", db=scope["db"],
    )
    from tests.registry_helpers import make_session

    scope["db"].execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("project-other", "Other", "workspace://workspace-stable-id/projects/other", "active", "t", "t"),
    )
    other = make_session(scope["db"], project_id="project-other", title="Other conductor", prefix="other-conductor")
    scope["db"].execute("UPDATE projects SET conductor_thread_id=? WHERE id='project-other'", (other,))
    scope["db"].commit()
    assert await _handover(artifact_id, other) == "Error: Ask the document's owner or their worker to give it to the person; for an unowned project document, ask its conductor."
    assert json.loads(await _handover(artifact_id, scope["conductor"]))["artifact"]
    assert _view(artifact_id)["owner"]["label"] == "You"


@pytest.mark.asyncio
async def test_project_conductor_registers_unowned_file_then_hands_it_to_person(assignment_scope):
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
