"""A fresh closing mutation emits one content-free `plans.close_next` event after commit.

The event measures the journey (follow-through, refusal mix, auto-selection)
without becoming state: it is emitted only after the mutation's write has
committed, only for fresh (non-replayed) closing mutations, and carries counts
and codes only - never an id, title or reason.
"""

from __future__ import annotations

import pytest

from plans import service
from tests.plan_close_next_helpers import (
    EVENT_PROPERTY_KEYS,
    HUMAN,
    EventCapture,
    close,
    close_params,
    owner_plan,
    plan_update,
    queue_active,
    receipt_count,
    select,
    serialized,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _scene(db):
    """Select Task A; queue active Task Q; leave Task B pending."""
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Queued work")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    queue_active(db, queued.id)
    return plan, task_a, queued, task_b


def test_one_event_is_emitted_after_commit_with_only_content_free_properties(db, monkeypatch):
    plan, task_a, queued, task_b = _scene(db)
    capture = EventCapture(db).install(monkeypatch)
    receipts_before = receipt_count(db)

    close(db, plan.id, "request_review", task_a.id, next_node_id=task_b.id)

    [event] = capture.close_events()
    assert event["in_transaction"] is False
    assert event["receipts"] == receipts_before + 1
    properties = event["properties"]
    assert set(properties) == EVENT_PROPERTY_KEYS
    assert properties["operation"] == "request_review"
    assert properties["named"] == "started"
    assert properties["selected_how"] == "started"
    assert (properties["open_total"], properties["open_listed"]) == (1, 1)
    assert properties["executor_is_actor"] is True
    text = serialized(properties)
    for node in (task_a, queued, task_b):
        assert node.id not in text
        assert node.title not in text


@pytest.mark.parametrize(
    ("next_node_id", "named", "selected_how"),
    [(None, "absent", "auto_selected"), ("no-such-node", "unknown", "auto_selected")],
    ids=["without-named", "refused-named"],
)
def test_the_event_names_the_named_outcome_and_how_the_slot_was_filled(
    db, monkeypatch, next_node_id, named, selected_how,
):
    plan, task_a, _queued, _task_b = _scene(db)
    capture = EventCapture(db).install(monkeypatch)
    extra = {} if next_node_id is None else {"next_node_id": next_node_id}

    close(db, plan.id, "request_review", task_a.id, **extra)

    [event] = capture.close_events()
    assert event["properties"]["named"] == named
    assert event["properties"]["selected_how"] == selected_how
    assert event["properties"]["open_total"] == 1


def test_an_empty_slot_reports_none_and_a_human_actor_is_not_the_executor(db, monkeypatch):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    select(db, plan.id, task_a.id)
    capture = EventCapture(db).install(monkeypatch)

    close(db, plan.id, "fail", task_a.id, actor=HUMAN)

    [event] = capture.close_events()
    assert event["properties"]["selected_how"] == "none"
    assert event["properties"]["executor_is_actor"] is False
    assert (event["properties"]["open_total"], event["properties"]["open_listed"]) == (0, 0)


class FailingEmitter(EventCapture):
    """An emitter that records the call and then fails, as a broken analytics store would."""

    def __call__(self, category, action, *args, **kwargs) -> None:
        """Record the event, then fail."""
        super().__call__(category, action, *args, **kwargs)
        raise RuntimeError("analytics store unavailable")


def test_a_failing_emitter_never_changes_the_committed_close(db, monkeypatch):
    plan, task_a, queued, _task_b = _scene(db)
    capture = FailingEmitter(db).install(monkeypatch)

    result = close(db, plan.id, "request_review", task_a.id)

    assert len(capture.close_events()) == 1
    assert result["next"]["selected"]["id"] == queued.id
    assert service._node_row(db, task_a.id)["state"] == "review_requested"


def test_a_replayed_close_emits_nothing(db, monkeypatch):
    plan, task_a, _queued, task_b = _scene(db)
    capture = EventCapture(db).install(monkeypatch)
    params = close_params(db, "request_review", task_a.id, next_node_id=task_b.id)

    plan_update(db, plan.id, "request_review", params, mutation_id="close-a")
    plan_update(db, plan.id, "request_review", params, mutation_id="close-a")

    assert len(capture.close_events()) == 1


def test_non_closing_and_refused_mutations_emit_nothing(db, monkeypatch):
    plan, task_a, _queued, task_b = _scene(db)
    capture = EventCapture(db).install(monkeypatch)

    plan_update(db, plan.id, "edit", {
        "node_id": task_b.id, "title": "Task B renamed",
        "expected_revision": service._node_row(db, task_b.id)["revision"],
    })
    with pytest.raises(service.PlanInputError):
        close(db, plan.id, "request_review", task_a.id, next_node_id=5)

    assert capture.close_events() == []
