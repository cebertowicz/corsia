"""A conductor's earlier foreign task cannot disclose its later private outcome."""

from infra.project_fence import WITHHELD_LOCAL_ONLY
from plans import closure, service
from tests.test_plan_conductor_task_adds import WORKSPACE_CONDUCTOR, _add, db  # noqa: F401


def test_conductor_task_outcome_obeys_current_fence(db):
    plan = service.ensure_plan("thread", "other-lane", db=db)
    node = _add(db, plan.id, WORKSPACE_CONDUCTOR)
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('project-b',1)")
    closure.close_node(node["id"], reason="Private reason", actor_id="other-lane", provenance="test", db=db)
    wake = db.execute(
        "SELECT content FROM messages WHERE thread_id='workspace-conductor' "
        "AND id LIKE 'plan-wake:conductor_task_settled:%'"
    ).fetchone()
    assert wake is not None
    assert wake["content"].startswith(WITHHELD_LOCAL_ONLY)
    assert "Private reason" not in wake["content"]
    assert "Refresh lane context" not in wake["content"]
