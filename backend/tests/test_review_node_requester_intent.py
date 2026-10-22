"""An escalated review task inherits the requesting task's canonical intent."""
from __future__ import annotations

from doceditor import review_requests as reviews
from plans import public
from test_doc_review_requests import review_env  # noqa: F401
from test_doc_review_requesting_node import _ask, _current, _no_fork_launch, _task  # noqa: F401


def test_an_escalated_review_points_at_the_requesters_canonical_intent(review_env):
    db, aid = review_env
    parent = _task(db, "requester", "Parent", intent="Parent intent")
    child = _task(db, "requester", "Child", intent_node_id=parent)
    result = _ask(aid, origin=_current(db, "requester", child))
    escalated = reviews.escalate_fork(db, result["id"])
    assert public.get_node(escalated["node_id"], db=db).intent_node_id == parent
    assert public.intent_of(escalated["node_id"], db=db)["node_id"] == parent


def test_a_request_without_a_requesting_task_leaves_the_review_without_intent(review_env):
    db, aid = review_env
    escalated = reviews.escalate_fork(db, _ask(aid)["id"])
    assert escalated["node_id"] is not None
    assert public.get_node(escalated["node_id"], db=db).intent_node_id is None


def _request(aid: str, request_id: str, **extra) -> dict:
    return {"id": request_id, "artifact_id": aid, "requester_display": "Requester", "note": None, **extra}


def test_an_unknown_requesting_task_still_creates_the_review(review_env):
    db, aid = review_env
    created = public.create_review_node(db, "reviewer", _request(aid, "r-gone", requesting_node_id="gone"))
    db.commit()
    assert created.created
    assert public.get_node(created.node_id, db=db).intent_node_id is None


def test_a_request_with_no_requesting_column_still_creates_the_review(review_env):
    db, aid = review_env
    created = public.create_review_node(db, "reviewer", _request(aid, "r-old"))
    db.commit()
    assert public.get_node(created.node_id, db=db).intent_node_id is None
