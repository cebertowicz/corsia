"""Person steps link owned instructions documents without creating a second ask."""
from __future__ import annotations

import pytest

from doc_feedback_helpers import seed_document, seed_thread
from plans import artifact_links, operator_chat_state, public, service
from plans.boundary_fields import PERSON_STEP_DESCRIPTION_LIMIT_REFUSAL
from tests.plan_close_next_helpers import HUMAN, OWNER, owner_plan, plan_update, task
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401


DESCRIPTION = "Run this setup. Already tried: I used the supported agent route."


def _document(db, owner: str = "owner") -> str:
    if owner != "owner":
        seed_thread(db, owner)
    return seed_document(db, owner, name=f"{owner}-instructions.md")


def _params(document: str | None = None) -> dict:
    return {
        "kind": "task", "title": "Set up the workspace", "acceptance_criteria": "It is ready.",
        "size": "unknown", "assignee": "user", "description": DESCRIPTION,
        **({"instructions_artifact_id": document} if document is not None else {}),
    }


def test_add_and_insert_link_an_owned_document_as_input(db):
    plan = owner_plan(db)
    document = _document(db)
    added = plan_update(db, plan.id, "add", _params(document))["node"]

    assert [link["role"] for link in artifact_links.list_for_node(added["id"], db=db)] == ["input"]
    assert service._node_row(db, added["id"])["assignee"] == "user"
    assert public.person_step_instructions(document, db=db)["node_id"] == added["id"]
    plan_update(db, plan.id, "activate", {
        "node_id": added["id"], "expected_revision": added["revision"],
    }, actor=HUMAN)
    plan_update(db, plan.id, "request_review", {
        "node_id": added["id"], "expected_revision": service._node_row(db, added["id"])["revision"],
        "evidence": "Ready for you.",
    }, actor=HUMAN)
    state = operator_chat_state.read_thread_states(["owner"], actor=HUMAN, db=db)["states"]["owner"]
    assert state["attention_items"] == [{"kind": "review", "title": "Set up the workspace", "node_id": added["id"]}]
    assert artifact_links.list_for_node(added["id"], db=db)[0]["attention_state"] is None

    anchor = task(db, plan.id, "Anchor")
    inserted = plan_update(
        db, plan.id, "insert", {**_params(document), "before_node_id": anchor.id},
    )["node"]
    assert [link["role"] for link in artifact_links.list_for_node(inserted["id"], db=db)] == ["input"]


@pytest.mark.parametrize("settled", [
    "state='accepted_with_followup'", "archived_at='2026-10-22T00:00:00+00:00'",
])
def test_a_settled_or_archived_person_step_no_longer_asks_from_its_document(db, settled):
    plan = owner_plan(db)
    document = _document(db)
    added = plan_update(db, plan.id, "add", _params(document))["node"]
    assert public.person_step_instructions(document, db=db)["node_id"] == added["id"]

    with raw_plan_seed(db):
        db.execute(f"UPDATE plan_nodes SET {settled} WHERE id=?", (added["id"],))

    assert public.person_step_instructions(document, db=db) is None


@pytest.mark.parametrize("params, message", [
    ({"instructions_artifact_id": "missing"}, "names no registered document"),
    ({"assignee": None, "instructions_artifact_id": "unused"}, "only valid for an assignee: user task"),
])
def test_instructions_document_refusals_are_self_describing(db, params, message):
    plan = owner_plan(db)
    values = _params()
    values.update(params)
    with pytest.raises(service.PlanInputError, match=message):
        plan_update(db, plan.id, "add", values)


def test_instructions_document_must_be_owned_by_the_calling_thread(db):
    plan = owner_plan(db)
    document = _document(db, "other")
    with pytest.raises(service.PlanInputError, match="document this thread owns"):
        plan_update(db, plan.id, "add", _params(document))


def test_person_step_description_is_limited_to_400_characters(db):
    plan = owner_plan(db)
    description = "x" * 380 + " Already tried: route"
    assert len(description) > 400
    with pytest.raises(service.PlanInputError, match="Put the how-to in a document"):
        plan_update(db, plan.id, "add", {**_params(), "description": description})
    assert PERSON_STEP_DESCRIPTION_LIMIT_REFUSAL == (
        "Put the how-to in a document, register it and pass instructions_artifact_id; "
        "keep the description to one line."
    )
