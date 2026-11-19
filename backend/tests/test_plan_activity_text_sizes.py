"""plan_activity reports how long plan text is, per field, and never the text."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from infra.audit_plan_activity import _summary, plan_activity
from plans import holds, service
from tests.test_plan_conductor_task_adds import db  # noqa: F401

SECRET = "PLAN_TEXT_SENTINEL"


def _window(*, days: int = 0) -> tuple[str, str]:
    now = datetime.now(timezone.utc) + timedelta(days=days)
    return (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat()


def _node(db, thread_id: str = "lane", **fields):
    plan = service.ensure_plan("thread", thread_id, db=db)
    return service.add_node(plan.id, kind="task", title="Work", acceptance_criteria="Done", db=db, **fields)


def _seed_every_field(db) -> None:
    node = _node(db, intent=SECRET + "i" * 40)
    _node(db, assignee="user", description=SECRET + " how-to. Already tried: the browser.")
    holds.place_hold(
        node.id, kind="paused", reason_key="test", reason=SECRET + "h" * 10, owner_kind="user",
        owner_id=None, expected_revision=node.revision, actor_id="lane", provenance="test", db=db,
    )
    for payload in ({"blocked": {"reason": SECRET + "b"}}, {"scheduled": {"reason": SECRET + "é"}}):
        service._event(db, node.id, "watch_armed", actor_id="lane", provenance="test", payload=payload)
    service._event(db, node.id, "review_rejected", actor_id="lane", provenance="test",
                   payload={"feedback": SECRET + "f" * 100})
    for size in (1, 2, 3):
        service._event(db, node.id, "review_evidence_submitted", actor_id="lane", provenance="test",
                       payload={"evidence": "e" * size})
    db.commit()


def test_every_field_reports_sizes_and_no_text(db):
    _seed_every_field(db)

    row = plan_activity(db, "user", *_window())[0]
    sizes = row["text_sizes"]

    assert SECRET not in json.dumps(row)
    assert "text_sizes" in row["definitions"]
    assert {field: sizes[field]["n"] for field in sizes} == {
        "hold_reason": 1, "blocked_wait_reason": 1, "set_aside_reason": 1, "review_feedback": 1,
        "review_evidence": 3, "intent": 1, "person_step_description": 1,
    }
    assert sizes["review_feedback"]["chars"]["max"] == len(SECRET) + 100
    assert sizes["review_evidence"]["chars"] == {"p50": 2, "p90": 3, "p99": 3, "max": 3}
    # A non-ASCII character is one character but six bytes once a tool emits it.
    assert sizes["set_aside_reason"]["chars"]["max"] == len(SECRET) + 1
    assert sizes["set_aside_reason"]["json_bytes"]["max"] == len(SECRET) + 1 + 5 + 2


def test_text_outside_the_window_or_for_another_user_is_not_counted(db):
    _seed_every_field(db)

    later = plan_activity(db, "user", *_window(days=2))[0]["text_sizes"]
    other = plan_activity(db, "someone-else", *_window())[0]["text_sizes"]

    for sizes in (later, other):
        assert all(field["n"] == 0 and field["chars"]["max"] is None for field in sizes.values())


def test_percentiles_are_nearest_rank():
    assert _summary(list(range(1, 101))) == {"p50": 50, "p90": 90, "p99": 99, "max": 100}
    assert _summary([7]) == {"p50": 7, "p90": 7, "p99": 7, "max": 7}
    assert _summary([]) == {"p50": None, "p90": None, "p99": None, "max": None}
