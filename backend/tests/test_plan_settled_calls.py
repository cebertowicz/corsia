"""The person's settled calls for a thread: answered decisions and ask verdicts.

Every row is made by its real producer: ``plans.decisions.answer`` for a
decision, and the review bar's ``attention.clear_attention`` for an ask verdict.
"""

from __future__ import annotations

import uuid

from plans import public, service
from plans.boundary import PlanActor, mutate
from registry import attention, models
from tests.test_plan_decisions import (  # noqa: F401 - shared fixtures
    _draft_kwargs,
    _mid,
    _task,
    db,
)


def _actor(thread_id: str) -> PlanActor:
    return PlanActor(thread_id, "user-1", "project-1")


def _human() -> PlanActor:
    return PlanActor(None, "user-1", None, kind="user")


def _raised(db, thread_id: str, title: str) -> dict:
    from plans import decisions

    plan = service.ensure_plan("thread", thread_id, db=db)
    source = _task(plan.id, f"Work for {title}", db=db)
    db.commit()
    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=_mid(), actor=_actor(thread_id),
        provenance="test", db=db, **_draft_kwargs(title=title),
    ).result["decision"]
    for step in (decisions.parent_review, decisions.raise_to_user):
        decision = step(
            decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
            actor=_actor(thread_id), provenance="test", db=db,
        ).result["decision"]
    db.commit()
    return decision


def _answered(db, thread_id: str, title: str, **answer) -> dict:
    from plans import decisions

    decision = _raised(db, thread_id, title)
    decision = decisions.answer(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_human(), provenance="test", db=db, **answer,
    ).result["decision"]
    db.commit()
    return decision


def _ask(db, thread_id: str, name: str) -> str:
    """Block a task in ``thread_id``'s plan on the person reviewing a new document."""
    artifact = models.register_artifact(
        integration="local", type="document", external_id=f"/workspace/shared/{name}",
        parent_thread_id=thread_id, title=name, db=db,
    )
    artifact_id = artifact["id"] if isinstance(artifact, dict) else artifact
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = _task(plan.id, f"Review {name}", db=db)
    db.commit()
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": f"Please read {name}",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": artifact_id,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=_actor(thread_id), provenance="test", mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()
    return artifact_id


def test_an_answered_decision_reaches_the_thread_and_the_workers_under_it(db):
    decision = _answered(db, "owner", "Which store?", option_id="B")

    expected = {
        "kind": "decision", "id": decision["id"], "question": "Which store?",
        "answer": "Reuse the identity", "note": None, "thread_id": "owner",
    }
    for thread_id, chain in (("owner", False), ("worker", True)):
        rows = public.settled_calls(db, thread_id, include_delegating_chain=chain)
        assert [{k: v for k, v in row.items() if k != "answered_at"} for row in rows] == [expected]
        assert rows[0]["answered_at"]
    assert public.settled_calls(db, "worker", include_delegating_chain=False) == []
    assert public.settled_calls(db, "cousin", include_delegating_chain=True)[0]["id"] == decision["id"]


def test_an_open_decision_is_no_settled_call(db):
    _raised(db, "owner", "Still open?")

    assert public.settled_calls(db, "owner", include_delegating_chain=True) == []


def test_an_other_answer_carries_its_text_as_the_note(db):
    _answered(db, "owner", "Which way?", option_id="other", other_text="Split it.")

    (row,) = public.settled_calls(db, "owner", include_delegating_chain=False)
    assert (row["answer"], row["note"]) == (None, "Split it.")


def test_ask_verdicts_join_decisions_newest_first_and_an_ask_ended_without_one_is_left_out(db):
    first = _answered(db, "owner", "Which store?", option_id="A")
    approved = _ask(db, "owner", "proposal.md")
    attention.clear_attention(approved, outcome="answer", verdict="approved", note="Ship it.", db=db)
    db.commit()
    replaced = _ask(db, "worker", "draft.md")
    attention.clear_attention(replaced, outcome="supersede", db=db)
    db.commit()

    rows = public.settled_calls(db, "worker", include_delegating_chain=True)

    assert [(row["kind"], row["id"]) for row in rows] == [
        ("ask_verdict", approved), ("decision", first["id"]),
    ]
    verdict = rows[0]
    assert (verdict["question"], verdict["answer"], verdict["note"], verdict["thread_id"]) == (
        "proposal.md", "approved", "Ship it.", "owner",
    )
    assert rows[0]["answered_at"] >= rows[1]["answered_at"]


def test_a_decision_body_missing_a_required_field_is_left_out(db):
    import json

    decision = _answered(db, "owner", "Which store?", option_id="A")
    row = db.execute(
        "SELECT id,metadata FROM artifact_identities WHERE external_id=?", (decision["id"],),
    ).fetchone()
    metadata = json.loads(row["metadata"])
    del metadata["plan_decision"]["tradeoff"]
    db.execute("UPDATE artifact_identities SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"]))
    db.commit()

    assert public.settled_calls(db, "owner", include_delegating_chain=False) == []



def test_a_corrupt_ask_answer_raises_rather_than_reading_as_no_answer(db):
    """Events are append-only, so the reader is fed the stored payload of a real answer, then a corrupt one."""
    import json

    import pytest

    from plans.user_ask_release import PROVENANCE, person_answer

    artifact_id = _ask(db, "owner", "proposal.md")
    attention.clear_attention(artifact_id, outcome="answer", verdict="approved", db=db)
    db.commit()
    (stored,) = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE event='hold_cleared' AND provenance=?", (PROVENANCE,),
    ).fetchone()

    assert person_answer(stored)["verdict"] == "approved"
    assert person_answer(None) is None
    with pytest.raises(json.JSONDecodeError):
        person_answer(stored[:-1])
