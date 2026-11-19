"""Review asks follow the person-owned document's effective supervisor."""

import pytest

from tests.test_agent_person_handover import _handover
from tests.test_document_owner_supervisor import _add_agent, _one_person_account
from tests.test_document_owner_supervisor import owned_document as owned_document


def _ask(db, artifact_id, actor_id):
    from plans import service, user_ask
    from plans.boundary import PlanActor

    thread = db.execute("SELECT user_id, project_id FROM threads WHERE id=?", (actor_id,)).fetchone()
    plan = service.ensure_plan("thread", actor_id, db=db)
    return user_ask.require(
        db, {"id": plan.id, "owner_id": actor_id}, "diagnostic-node",
        {"ask_artifact_id": artifact_id}, PlanActor(actor_id, thread["user_id"], thread["project_id"]),
    )


@pytest.mark.asyncio
async def test_handover_ask_follows_current_supervisor(owned_document):
    from infra.database import get_db
    from plans import public, service
    from plans.boundary import PlanActor
    from registry import models

    client, artifact_id = owned_document
    _one_person_account()
    _add_agent("agent-2", "Other agent")
    db = get_db()
    try:
        # Agent-owned documents still admit only their owner.
        assert _ask(db, artifact_id, "agent-1") == artifact_id
        with pytest.raises(service.PlanInputError, match="must name an artifact this thread owns, or a person-owned document this thread supervises"):
            _ask(db, artifact_id, "agent-2")

        await _handover(artifact_id, "agent-1")
        assert _ask(db, artifact_id, "agent-1") == artifact_id
        with pytest.raises(service.PlanInputError, match="must name an artifact this thread owns, or a person-owned document this thread supervises"):
            _ask(db, artifact_id, "agent-2")

        thread = db.execute("SELECT user_id, project_id FROM threads WHERE id=?", ("agent-1",)).fetchone()
        plan = service.ensure_plan("thread", "agent-1", db=db)
        node = service.add_node(
            plan.id, kind="task", title="Review document", acceptance_criteria="Reviewed",
            actor_id=None, provenance="test", db=db,
        )
        public.mutate(
            plan.id, "block_node",
            {"node_id": node.id, "expected_revision": node.revision, "owner_kind": "user",
             "ask_artifact_id": artifact_id, "reason_key": "review", "reason": "Please review"},
            actor=PlanActor("agent-1", thread["user_id"], thread["project_id"]),
            provenance="test", mutation_id="handover-ask", db=db,
        )
        response = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"supervisor_thread_id": "agent-2"})
        assert response.status_code == 200, response.text
        with pytest.raises(service.PlanInputError, match="must name an artifact this thread owns, or a person-owned document this thread supervises"):
            _ask(db, artifact_id, "agent-1")
        assert _ask(db, artifact_id, "agent-2") == artifact_id

        # Project-owned documents follow the project's conductor, not a stored person supervisor.
        db.execute("INSERT INTO projects(id,name,conductor_thread_id,created_at,updated_at) VALUES ('project-alpha','Alpha','agent-1','t','t')")
        artifact = models.get_artifact(artifact_id, db=db)
        models.rehome_artifact(
            integration=artifact["integration"], type=artifact["type"], external_id=artifact["external_id"],
            new_owner_thread_id="project:project-alpha", actor_thread_id=None, authority="system",
            reason="test", source_path="test", db=db,
        )
        assert _ask(db, artifact_id, "agent-1") == artifact_id
        with pytest.raises(service.PlanInputError, match="or a project-owned document whose conductor is this thread"):
            _ask(db, artifact_id, "agent-2")
    finally:
        db.close()
