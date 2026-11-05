"""Editing inherited intent gives the node its own canonical intent."""
from plans import intent_creation, service
from plans.boundary import mutate
from tests.plan_scene import ACTOR, db  # noqa: F401


def test_editing_the_intent_of_an_inheriting_node_makes_it_own_its_intent(db):
    plan = service.ensure_plan("thread", "root", db=db)
    canonical = service.add_node(
        plan.id, kind="task", title="Canonical", acceptance_criteria="Kept", intent="The shared goal", db=db,
    )
    inheriting = service.add_node(
        plan.id, kind="task", title="Inherits", acceptance_criteria="Follows", intent_node_id=canonical.id, db=db,
    )
    assert intent_creation.canonical(db, inheriting.id)["id"] == canonical.id

    mutate(
        plan.id, "edit", {
            "node_id": inheriting.id, "intent": "A goal of its own", "size": "S",
            "expected_revision": service.get_node(inheriting.id, db=db).revision,
        }, actor=ACTOR, provenance="test", mutation_id="own-intent", db=db,
    )

    owned = intent_creation.canonical(db, inheriting.id)
    assert owned["id"] == inheriting.id and owned["intent"] == "A goal of its own"
    assert service.get_node(inheriting.id, db=db).intent_node_id is None
