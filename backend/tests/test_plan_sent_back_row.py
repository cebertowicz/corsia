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


def _closed(db, plan_id: str, title: str):
    from plans import closure

    node = _task(db, plan_id, title=title)
    service.transition_node(node.id, "active", db=db)
    closure.close_node(node.id, reason="Not needed", db=db)
    return node


def test_the_stored_mark_alone_makes_a_round_in_every_read(db):
    """``plan_nodes.sent_back`` is the one store; the event only carries the feedback text.

    The one writer (``rejection._send_back``) writes both, so they can only disagree
    through a defect. Pull them apart on purpose: the pane and the full rollup must
    both follow the stored mark, never the ``review_rejected`` event.
    """
    import uuid

    from plans import rollup
    from tests.plan_writer_authorizer import raw_plan_seed

    plan = _plan(db)
    stray = _closed(db, plan.id, "Event only")
    _closed(db, plan.id, "Ordinary close")
    marked = _closed(db, plan.id, "Mark only")
    with raw_plan_seed(db):
        db.execute(
            """INSERT INTO plan_node_events(id,node_id,provenance,event,payload_json,created_at)
               VALUES (?,?,'test','review_rejected','{"feedback":"stray"}','2026-10-11T00:00:00')""",
            (uuid.uuid4().hex, stray.id),
        )
        db.execute("UPDATE plan_nodes SET sent_back=1 WHERE id=?", (marked.id,))
    db.commit()

    pane = {row["title"]: row for row in _pane(db)}
    full = {node["title"] for node in rollup.rollup_thread(plan.id, db=db).nodes}

    assert set(pane) == full == {"Event only", "Ordinary close"}
    assert pane["Event only"]["status_reason"] == "Not needed"

    # Put the two stores back in agreement: the teardown recount derives the mark
    # from the events and must judge a plan the one writer could have produced.
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET sent_back=1 WHERE id=?", (stray.id,))
        db.execute("UPDATE plan_nodes SET sent_back=0 WHERE id=?", (marked.id,))
    db.commit()


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
