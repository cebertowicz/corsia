"""Sent-back work reads as one open task, never as a closed copy of itself.

A rejection closes the reviewed round and creates its revision beside it
(``plans.rejection``). The pane and the status digest show the task once: the
revision, carrying the feedback it was sent back with.
"""

from __future__ import annotations

from plans import operator_projection, rejection, service
from tests.plan_review_helpers import submit_review_evidence
from tests.test_plan_archival import _plan, _task, db  # noqa: F401 - fixture
from tests.test_plan_task_steering_http import USER

FEEDBACK = "Missing the cost section"


def _sent_back(db, plan_id: str):
    node = _task(db, plan_id, title="Write the brief")
    service.transition_node(node.id, "active", db=db)
    db.commit()
    submit_review_evidence(node.id, db=db)
    db.commit()
    rejection.reject_review(node.id, feedback=FEEDBACK, db=db)
    db.commit()
    return node


def _pane(db) -> list[dict]:
    return operator_projection.read_thread("root", actor=USER, db=db)["plans"][0]["nodes"]


def test_sent_back_work_shows_once_and_open(db):
    plan = _plan(db)
    submission = _sent_back(db, plan.id)

    rows = _pane(db)

    assert [row["title"] for row in rows] == ["Write the brief"]
    (row,) = rows
    assert row["id"] != submission.id
    assert row["state"] == "pending"
    assert row["status_reason"] == f"Sent back: {FEEDBACK}"
    assert not any(r["display"]["label_key"] == "finished" for r in rows)


def test_a_second_send_back_still_shows_one_row(db):
    plan = _plan(db)
    _sent_back(db, plan.id)
    (revision,) = _pane(db)
    service.transition_node(revision["id"], "active", db=db)
    db.commit()
    submit_review_evidence(revision["id"], db=db)
    db.commit()
    rejection.reject_review(revision["id"], feedback="Still no totals", db=db)
    db.commit()

    (row,) = _pane(db)

    assert row["status_reason"] == "Sent back: Still no totals"
    assert row["execution"]["friction_count"] == 2
    assert row["execution"]["revision_round"] == 2


def test_the_stored_row_fact_alone_makes_a_round():
    """``plan_nodes.sent_back`` is the one store; the event only carries the feedback text.

    No runtime read decides membership from the ``review_rejected`` event: the one
    predicate that joins it requires the stored mark. The recount oracle
    (``aggregates_recount``) derives the mark from the events on purpose, to prove the
    one writer (``rejection._send_back``) wrote both.
    """
    import re
    from pathlib import Path

    from plans import rollup

    assert rollup.round_feedback_sql("n", "e").startswith("n.sent_back=1 AND ")
    plans_dir = Path(rollup.__file__).parent
    readers = {
        path.name: len(re.findall(r"event\s*=\s*'review_rejected'", path.read_text()))
        for path in plans_dir.glob("*.py")
    }
    assert {name: n for name, n in readers.items() if n} == {
        "rollup.py": 1, "aggregates_recount.py": 1,
    }


def test_an_ordinary_close_still_shows_closed(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Dropped")
    from plans import closure

    service.transition_node(node.id, "active", db=db)
    closure.close_node(node.id, reason="Not needed", db=db)
    db.commit()

    (row,) = _pane(db)

    assert row["display"]["label_key"] == "finished"
    assert row["status_reason"] == "Not needed"
