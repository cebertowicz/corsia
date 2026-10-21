"""A review request carries one server-resolved requester task (AL5 seam 2)."""
from __future__ import annotations

import pytest

from doceditor import review_requests as reviews
from plans import admission, intent_words, mutations, public, service
from registry.registration_origin import RegistrationOrigin
from test_doc_review_requests import review_env, stored  # noqa: F401
from test_review_fork_cold_request import TARGET, _prepare, _turn
from tests.plan_execution_helpers import open_turn
from tools import document_reviews
from tools.document_reviews import create_request


@pytest.fixture(autouse=True)
def _no_fork_launch(monkeypatch):
    """These tests cover what the request stores and the input it builds, not a provider run."""
    monkeypatch.setattr(document_reviews, "_start_after_commit", lambda request_id: None)


def _task(db, thread: str, title: str = "Ship it", **kwargs) -> str:
    plan = service.ensure_plan("thread", thread, db=db)
    node = service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria=f"{title} secret criteria", db=db, **kwargs,
    )
    service.transition_node(node.id, "active", db=db)
    db.commit()
    return node.id


def _current(db, thread: str, node_id: str) -> RegistrationOrigin:
    revision = service.get_node(node_id, db=db).revision
    admission.start(node_id, expected_revision=revision, actor_id=thread, provenance="test", db=db)
    db.commit()
    return RegistrationOrigin(thread, open_turn(db, thread).turn_id, "test")


def _actor(kind: str = "agent") -> reviews.ReviewActor:
    if kind == "user":
        return reviews.ReviewActor("user", "owner-token", "requester", "owner-token", "Steve")
    return reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")


def _ask(aid, *, key="k1", note="Check claims", kind="agent", origin=None):
    result, _woken = create_request(
        _actor(kind), reviewer="reviewer", note=note, client_id=key, artifact_id=aid, origin=origin,
    )
    return result


def _request_with_live_node(db, aid, node: str, *, key="k1", note="Check claims"):
    return reviews.request_review(
        db, aid, "reviewer", _actor(), note, key, live_node_id=node,
    )


def test_the_turns_live_node_is_stored(review_env):
    db, aid = review_env
    node = _task(db, "requester")
    result = _ask(aid, origin=_current(db, "requester", node))
    assert result["state"] == "forked" and result["requesting_node_id"] == node


def test_no_live_node_is_null(review_env):
    db, aid = review_env
    _task(db, "requester")
    assert _ask(aid)["requesting_node_id"] is None


def test_a_person_request_is_null_even_from_a_thread_with_a_live_node(review_env):
    db, aid = review_env
    node = _task(db, "requester")
    origin = _current(db, "requester", node)
    assert _ask(aid, kind="user", origin=origin)["requesting_node_id"] is None


def _words(db, node: str) -> None:
    mutations.edit_node(
        node, expected_revision=service.get_node(node, db=db).revision, actor_id="requester", db=db,
        intent_words=[{
            "quote": "Keep the claims honest", "author": "agent", "role": "bears_on",
            "source_kind": "message", "source_id": "requester",
        }],
    )
    db.commit()


def test_the_fork_input_carries_the_task_and_revision_read_not_a_plan_read_line(review_env, monkeypatch):
    db, aid = review_env
    node = _task(db, "requester", "Ship it", intent="Preserve the outcome")
    _words(db, node)
    result = _ask(aid, origin=_current(db, "requester", node))
    _turn(db, "completed", "completed", messages=True)
    instruction = _prepare(monkeypatch, result["id"])[1].instruction
    revision = service.get_node(node, db=db).requirements_revision
    assert f'Requested from task "Ship it" ({node}), requirements revision {revision}.' in instruction
    assert "Intent: Preserve the outcome" in instruction
    assert "Acceptance criteria: Ship it secret criteria" in instruction
    assert 'agent (bears_on): "Keep the claims honest"' in instruction
    assert "Check claims" in instruction and "plan_read" not in instruction


def test_the_fork_reads_the_revision_current_at_fork_start(review_env, monkeypatch):
    db, aid = review_env
    node = _task(db, "requester", "Ship it", intent="Preserve the outcome")
    result = _ask(aid, origin=_current(db, "requester", node))
    first = service.get_node(node, db=db).requirements_revision
    mutations.edit_node(
        node, expected_revision=service.get_node(node, db=db).revision, actor_id="requester", db=db,
        acceptance_criteria="Revised criteria",
    )
    db.commit()
    bumped = service.get_node(node, db=db).requirements_revision
    assert bumped == first + 1
    _turn(db, "completed", "completed", messages=True)
    instruction = _prepare(monkeypatch, result["id"])[1].instruction
    assert f"requirements revision {bumped}." in instruction and "Acceptance criteria: Revised criteria" in instruction


def test_an_overlong_criteria_text_is_bounded_in_the_fork_input():
    from prompts.artifact_review import MAX_CRITERIA_BYTES, requesting_task_block

    block = requesting_task_block({
        "node_id": "n", "title": "T", "requirements_revision": 1, "intent": None,
        "acceptance_criteria": "é" * 5000, "intent_words": [],
    })
    assert "Intent:" not in block and block.count("é") == MAX_CRITERIA_BYTES // 2
    assert block.endswith(" [truncated]")
    whole = requesting_task_block({
        "node_id": "n", "title": "T", "requirements_revision": 1, "intent": None,
        "acceptance_criteria": "short", "intent_words": [],
    })
    assert whole.endswith("Acceptance criteria: short")


def test_a_failed_task_read_fails_the_fork_start_instead_of_dropping_the_task(review_env, monkeypatch):
    db, aid = review_env
    node = _task(db, "requester")
    result = _ask(aid, origin=_current(db, "requester", node))

    def gone(*_args, **_kwargs):
        raise public.PlanError("unknown plan node")

    monkeypatch.setattr(public, "intent_of", gone)
    _turn(db, "completed", "completed", messages=True)
    with pytest.raises(public.PlanError):
        _prepare(monkeypatch, result["id"])


def test_a_fork_input_without_a_requesting_task_has_no_task_block(review_env, monkeypatch):
    db, aid = review_env
    result = _ask(aid)
    _turn(db, "completed", "completed", messages=True)
    assert "Requested from task" not in _prepare(monkeypatch, result["id"])[1].instruction


def test_escalation_passes_the_requesting_node_through_unchanged(review_env, monkeypatch):
    db, aid = review_env
    node = _task(db, "requester", intent="Preserve the outcome")
    result = _ask(aid, origin=_current(db, "requester", node))
    seen = []
    create = public.create_review_node

    def capture(connection, reviewer, request):
        seen.append(request)
        return create(connection, reviewer, request)

    monkeypatch.setattr(public, "create_review_node", capture)
    escalated = reviews.escalate_fork(db, result["id"])
    assert escalated["requesting_node_id"] == node
    description = public.get_node(escalated["node_id"], db=db).description
    assert seen[0]["requesting_node_id"] == node
    assert "Check claims" in description
    assert "Requested from task" not in description and "Preserve the outcome" not in description


def test_a_pre_migration_row_reads_as_null_through_escalation(review_env):
    db, aid = review_env
    result = _ask(aid)
    db.execute("UPDATE doc_review_requests SET requesting_node_id=NULL WHERE id=?", (result["id"],))
    db.commit()
    escalated = reviews.escalate_fork(db, result["id"])
    assert escalated["requesting_node_id"] is None
    assert public.get_node(escalated["node_id"], db=db).intent_node_id is None


def test_a_repeat_from_another_task_is_refused_while_queued(review_env):
    db, aid = review_env
    first, second = _task(db, "requester", "First"), _task(db, "requester", "Second")
    opened = _ask(aid, origin=_current(db, "requester", first))
    # The adapter committed the request but this module's autouse fixture held its fork launch.
    with pytest.raises(reviews.ReviewRequestError, match="another task"):
        _request_with_live_node(db, aid, second, key="k2", note=None)
    assert stored(db, opened["id"])["requesting_node_id"] == first


def test_a_repeat_from_another_task_is_refused_while_forked(review_env):
    db, aid = review_env
    first, second = _task(db, "requester", "First"), _task(db, "requester", "Second")
    _request_with_live_node(db, aid, first)
    with pytest.raises(reviews.ReviewRequestError, match="another task"):
        _request_with_live_node(db, aid, second, key="k2", note=None)


def test_a_repeat_from_the_same_task_or_none_joins(review_env):
    db, aid = review_env
    first = _task(db, "requester", "First")
    opened = _request_with_live_node(db, aid, first)
    assert _request_with_live_node(db, aid, first, key="k2", note=None)["id"] == opened["id"]
    assert reviews.request_review(db, aid, "reviewer", _actor(), None, "k3")["id"] == opened["id"]
    with pytest.raises(reviews.ReviewRequestError, match="already in progress"):
        _request_with_live_node(db, aid, first, key="k4", note="Also this")


def test_a_repeat_from_another_task_is_refused_after_escalation(review_env):
    db, aid = review_env
    first, second = _task(db, "requester", "First"), _task(db, "requester", "Second")
    opened = _request_with_live_node(db, aid, first)
    reviews.escalate_fork(db, opened["id"])
    with pytest.raises(reviews.ReviewRequestError, match="another task"):
        _request_with_live_node(db, aid, second, key="k2", note=None)


def test_the_fork_and_the_inherited_task_name_the_same_intent_source(review_env, monkeypatch):
    db, aid = review_env
    parent = _task(db, "requester", "Parent", intent="Parent intent")
    child = _task(db, "requester", "Child", intent_node_id=parent)
    result = _ask(aid, origin=_current(db, "requester", child))
    _turn(db, "completed", "completed", messages=True)
    instruction = _prepare(monkeypatch, result["id"])[1].instruction
    canonical = intent_words.canonical(db, child)
    assert canonical["id"] == parent
    assert f'Requested from task "Parent" ({parent})' in instruction
