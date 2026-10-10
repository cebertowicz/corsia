"""Every event wait ends: 24 hours by default, 7 days at most (W4).

``shared/planning/plans-work-never-stops-2026-09-28.md``. A task waiting on an
artifact's event used to wait on the event alone, and an event that never came
left the task parked with nothing that would ever look at it again.

- An event wait armed with no deadline gets one 24 hours out. It is still the
  same wait: the event ends it the moment it arrives.
- A deadline more than 7 days out is cut to 7 days, and the reply says so.
- A scheduled task is a date the agent chose and keeps any distance.
- When the deadline passes, the wake says the event did not arrive and what to
  do about it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from events.identity import GITHUB_INTEGRATION, PR_TYPE
from infra.schema import SCHEMA_SQL
from plans import service, watch_resolution
from plans.boundary import PlanActor, mutate
from tests.plan_scene import _artifact

OWNER = PlanActor("owner", "user", "project")
PR = "owner/repo#7"
CHECKS = "github.check_suite.completed"


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('owner','user','project','test/model','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def _arm(db, **params) -> tuple[str, dict]:
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Land the PR", acceptance_criteria="Merged", db=db,
    )
    db.commit()
    result = mutate(
        plan.id, "arm_watch",
        {
            "node_id": node.id,
            "expected_revision": service._node_row(db, node.id)["revision"],
            **params,
        },
        actor=OWNER, provenance="test.event_wait", mutation_id=uuid.uuid4().hex, db=db,
    ).result
    db.commit()
    return node.id, result


def _event_wait(db, **extra) -> tuple[str, dict]:
    return _arm(
        db, condition={"type": "event", "value": "ci.complete"},
        artifact_id=_artifact(db, external_id=PR, owner="owner"),
        expected_event=CHECKS, **extra,
    )


def _armed(db, node_id: str) -> sqlite3.Row:
    [row] = db.execute(
        """SELECT * FROM expected_event_watches
           WHERE plan_node_id=? AND status='armed'""",
        (node_id,),
    ).fetchall()
    return row


def test_an_event_wait_with_no_deadline_ends_in_24_hours(db):
    before = datetime.now(timezone.utc)

    node_id, result = _event_wait(db)

    watch = _armed(db, node_id)
    due = datetime.fromisoformat(watch["condition_value"])
    assert watch["condition_type"] == "deadline"
    assert before + timedelta(hours=24) <= due <= datetime.now(timezone.utc) + timedelta(hours=24)
    # Still the same wait: the artifact and the event that ends it are kept.
    assert (watch["artifact_external_id"], watch["expected_event"]) == (PR, CHECKS)
    # The reply says when it ends and how to end it sooner.
    assert watch["condition_value"] in result["wait"]
    assert CHECKS in result["wait"] and "resolve_watch" in result["wait"]



def test_an_event_wait_row_names_its_event_and_when_it_ends(db):
    from plans import blocked_wait, node_waits

    node_id, _result = _event_wait(db)

    [row] = node_waits.armed_watches(db, [node_id])[node_id]
    reason = blocked_wait.watch_reason(row)
    assert reason == f"Waiting for {CHECKS} until {row['condition_value']}"

def test_the_event_still_ends_the_wait_before_its_deadline(db):
    node_id, _result = _event_wait(db)
    watch_id = _armed(db, node_id)["id"]

    resolved = watch_resolution.resolve_delivered_event(
        SimpleNamespace(integration=GITHUB_INTEGRATION, type=PR_TYPE, external_id=PR),
        CHECKS, recipient="owner", db=db,
    )

    assert resolved == [watch_id]


def test_arming_the_same_event_wait_again_keeps_one_watch(db):
    node_id, first = _event_wait(db)
    again = mutate(
        str(service._node_row(db, node_id)["plan_id"]), "arm_watch",
        {
            "node_id": node_id, "condition": {"type": "event", "value": "ci.complete"},
            "artifact_id": _artifact(db, external_id=PR, owner="owner"),
            "expected_event": CHECKS,
            "expected_revision": service._node_row(db, node_id)["revision"],
        },
        actor=OWNER, provenance="test.event_wait", mutation_id=uuid.uuid4().hex, db=db,
    ).result

    assert again["watch"]["id"] == first["watch"]["id"]


def test_a_deadline_past_7_days_is_cut_to_7_days(db):
    far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    node_id, result = _arm(
        db, condition={"type": "deadline", "value": far},
        artifact_id=_artifact(db, external_id=PR, owner="owner"),
        expected_event=CHECKS,
    )

    due = datetime.fromisoformat(_armed(db, node_id)["condition_value"])
    assert due <= datetime.now(timezone.utc) + timedelta(days=7)
    assert due > datetime.now(timezone.utc) + timedelta(days=6, hours=23)
    assert "7 days" in result["wait"]


def test_a_deadline_within_7_days_is_kept(db):
    soon = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)

    node_id, _result = _arm(
        db, condition={"type": "deadline", "value": soon.isoformat()},
        artifact_id=_artifact(db, external_id=PR, owner="owner"),
        expected_event=CHECKS,
    )

    assert datetime.fromisoformat(_armed(db, node_id)["condition_value"]) == soon


def test_a_scheduled_task_keeps_its_far_date(db):
    far = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0)

    node_id, _result = _arm(
        db, condition={"type": "deadline", "value": far.isoformat()},
        reason="Revisit after the quarter closes",
    )

    assert datetime.fromisoformat(_armed(db, node_id)["condition_value"]) == far


def test_the_expiry_wake_says_the_event_did_not_arrive(db):
    from automation.continuation.sources.watch_expiry import WatchExpirySource

    node_id, _result = _event_wait(db)
    later = (datetime.now(timezone.utc) + timedelta(hours=25)).isoformat()

    [request] = WatchExpirySource().due(later, db=db)

    summary = request.details["summary"]
    assert CHECKS in summary and "did not arrive" in summary
    assert "Land the PR" in summary and node_id in summary
    # What to do next, each by the call that does it.
    for way_out in ("arm_watch", "close_node", "decision"):
        assert way_out in summary
