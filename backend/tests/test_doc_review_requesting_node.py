"""A review request names its requester's task by reference (AL5 seam 2), on the fork-first base."""
from __future__ import annotations

import json

import pytest

from doc_feedback_helpers import seed_thread
from doceditor import review_requests as reviews
from plans import admission, mutations, public, service
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


def _ask(aid, *, key="k1", note="Check claims", kind="agent", **kwargs):
    result, _woken = create_request(_actor(kind), reviewer="reviewer", note=note, client_id=key, artifact_id=aid, **kwargs)
    return result


def test_an_explicit_node_is_stored(review_env):
    db, aid = review_env
    node = _task(db, "requester")
    result = _ask(aid, requesting_node_id=node)
    assert result["state"] == "forked" and result["requesting_node_id"] == node


def test_no_param_uses_the_turns_live_node(review_env):
    db, aid = review_env
    node = _task(db, "requester")
    assert _ask(aid, origin=_current(db, "requester", node))["requesting_node_id"] == node


def test_no_param_and_no_live_node_is_null(review_env):
    db, aid = review_env
    _task(db, "requester")
    assert _ask(aid)["requesting_node_id"] is None


def test_a_person_request_is_null_even_from_a_thread_with_a_live_node(review_env):
    db, aid = review_env
    node = _task(db, "requester")
    origin = _current(db, "requester", node)
    assert _ask(aid, kind="user", requesting_node_id=node, origin=origin)["requesting_node_id"] is None


@pytest.mark.parametrize("which", ["foreign", "missing"])
def test_an_unreadable_explicit_node_is_refused_not_nulled(review_env, which):
    db, aid = review_env
    seed_thread(db, "stranger")
    db.execute("UPDATE threads SET user_id='foreign' WHERE id='stranger'")
    node = _task(db, "stranger") if which == "foreign" else "no-such-node"
    with pytest.raises(reviews.ReviewRequestError, match="plan_read"):
        _ask(aid, requesting_node_id=node)
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0


def test_the_notice_names_the_task_by_reference_not_by_text(review_env):
    db, _aid = review_env
    node = _task(db, "requester")
    text = reviews.requesting_task_notice(db, node)
    assert text == (
        f"Requested from task {node}; read its current intent and acceptance criteria with "
        f"plan_read(thread_id='requester', node_id='{node}')."
    )
    assert "secret criteria" not in text


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
    result = _ask(aid, requesting_node_id=node)
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
    result = _ask(aid, requesting_node_id=node)
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
    result = _ask(aid, requesting_node_id=node)

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


def test_escalation_carries_the_row_value(review_env):
    db, aid = review_env
    node = _task(db, "requester", intent="Preserve the outcome")
    result = _ask(aid, requesting_node_id=node)
    escalated = reviews.escalate_fork(db, result["id"])
    assert escalated["requesting_node_id"] == node
    description = public.get_node(escalated["node_id"], db=db).description
    assert reviews.requesting_task_notice(db, node) in description and "Check claims" in description
    assert "Preserve the outcome" not in description  # by reference, never the text


def test_a_pre_migration_row_reads_as_null_through_escalation(review_env):
    db, aid = review_env
    result = _ask(aid)
    db.execute("UPDATE doc_review_requests SET requesting_node_id=NULL WHERE id=?", (result["id"],))
    db.commit()
    escalated = reviews.escalate_fork(db, result["id"])
    assert escalated["requesting_node_id"] is None
    assert public.get_node(escalated["node_id"], db=db).intent_node_id is None


def test_a_no_note_repeat_from_another_task_while_forked_joins_and_a_note_is_refused(review_env):
    db, aid = review_env
    first, second = _task(db, "requester", "First"), _task(db, "requester", "Second")
    opened = _ask(aid, requesting_node_id=first)
    assert _ask(aid, key="k2", note=None, requesting_node_id=first)["id"] == opened["id"]
    # Another task's no-note repeat joins the running review, as on dev: key recorded, nothing else changes.
    assert _ask(aid, key="k3", note=None, requesting_node_id=second)["id"] == opened["id"]
    # A fork's input is frozen, so a note is still refused, from any task.
    with pytest.raises(reviews.ReviewRequestError, match="already in progress"):
        _ask(aid, key="k4", note="Also this", requesting_node_id=second)
    row = stored(db, opened["id"])
    assert row["requesting_node_id"] == first and row["state"] == "forked"
    assert json.loads(row["client_ids"]) == ["k1", "k2", "k3"]
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE owner_thread_id='reviewer'").fetchone()[0] == 0


def test_a_repeat_from_another_task_after_escalation_names_it_on_the_review_task(review_env):
    db, aid = review_env
    first, second = _task(db, "requester", "First"), _task(db, "requester", "Second")
    opened = _ask(aid, requesting_node_id=first)
    node_id = reviews.escalate_fork(db, opened["id"])["node_id"]
    _ask(aid, key="k2", note=None, requesting_node_id=first)
    # Escalation named the first task; the same task's repeat adds nothing.
    assert public.get_node(node_id, db=db).description.count(f"Requested from task {first};") == 1
    _ask(aid, key="k3", note="Also this", requesting_node_id=second)
    row = stored(db, opened["id"])
    assert row["requesting_node_id"] == first
    description = public.get_node(node_id, db=db).description
    assert f"Requested from task {second}; read" in description
    assert f"node_id='{second}')" in description and "Also this" in description
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 1
