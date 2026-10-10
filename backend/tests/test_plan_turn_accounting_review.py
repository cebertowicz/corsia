"""Review verification attribution never refuses acceptance.

`review_usage.bind`, reached through the boundary's accept, attributes an
unbound live reviewer Turn to the verified commitment. A reviewer Turn that is missing, no longer live, or already bound to
another commitment is accounting state: accept succeeds and nothing is
attributed or rewritten.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.llm_usage import record_llm_usage
from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate
from tests.plan_execution_helpers import (
    add_task,
    finish_turn,
    open_turn,
    turn_accounting,
)

_REVIEW_TURN = "review-turn"
_PLAN_COLUMNS = (
    "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role"
)


class _KeepOpen:
    """Hand the test connection to usage recording without letting it close."""

    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def close(self):
        return None


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('owner','user','test/model','t','t')"
    )
    connection.commit()
    yield connection
    connection.close()


def _mutate(db, plan_id, operation, params, key, *, turn_id=None) -> dict:
    return mutate(
        plan_id,
        operation,
        params,
        actor=PlanActor("owner", "user", None, turn_id=turn_id),
        provenance="test.turn_accounting_review",
        mutation_id=key,
        db=db,
    ).result


def _in_review(db, plan_id):
    """Activate and submit one task before any reviewer Turn exists."""
    task = add_task(db, plan_id, "Reviewed work")
    active = _mutate(
        db, plan_id, "activate",
        {"node_id": task.id, "expected_revision": task.revision},
        "activate-reviewed",
    )["node"]
    reviewed = _mutate(
        db, plan_id, "request_review",
        {
            "node_id": task.id,
            "evidence": "Focused proof passed",
            "expected_revision": active["revision"],
        },
        "review-reviewed",
    )["node"]
    return task, reviewed


def _accept(db, plan_id, reviewed: dict) -> dict:
    return _mutate(
        db, plan_id, "accept",
        {
            "node_id": reviewed["id"],
            "verification_result": "passed",
            "expected_revision": reviewed["revision"],
        },
        "accept-reviewed",
        turn_id=_REVIEW_TURN,
    )


def _no_event(*_args, **_kwargs) -> None:
    return None


def _record_usage(db) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("infra.database.get_db", lambda: _KeepOpen(db))
        record_llm_usage(
            "owner",
            "test-model",
            {"total_tokens": 9},
            turn_id=_REVIEW_TURN,
            usage_key="provider-call:1",
            _emit_product_event=_no_event,
        )


def _usage_accounting(db) -> tuple:
    row = db.execute(
        f"SELECT {_PLAN_COLUMNS} FROM llm_usage WHERE turn_id=?", (_REVIEW_TURN,),
    ).fetchone()
    return tuple(row)


def test_unbound_reviewer_turn_binds_verification_and_backfills_usage(db):
    """An unbound reviewer Turn is bound for verification and its usage backfilled."""
    plan = service.ensure_plan("thread", "owner", db=db)
    task, reviewed = _in_review(db, plan.id)
    open_turn(db, "owner", _REVIEW_TURN)
    _record_usage(db)
    assert _usage_accounting(db) == (None, None, None, None, None)

    accepted = _accept(db, plan.id, reviewed)

    latest_attempt = db.execute(
        """SELECT id FROM plan_node_attempts
           WHERE node_id=? ORDER BY attempt_ordinal DESC LIMIT 1""",
        (task.id,),
    ).fetchone()["id"]
    expected = (plan.id, task.id, latest_attempt, task.commitment_id, "verification")
    assert accepted["node"]["state"] == "accepted"
    assert turn_accounting(db, _REVIEW_TURN) == expected
    assert _usage_accounting(db) == expected


def test_accept_succeeds_when_reviewer_turn_is_bound_to_another_commitment(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    task, reviewed = _in_review(db, plan.id)
    own = add_task(db, plan.id, "Reviewer's own work")
    open_turn(db, "owner", _REVIEW_TURN)
    _mutate(
        db, plan.id, "activate",
        {"node_id": own.id, "expected_revision": own.revision}, "activate-own",
    )
    _record_usage(db)
    snapshot = turn_accounting(db, _REVIEW_TURN)
    assert snapshot[3] == own.commitment_id != task.commitment_id

    accepted = _accept(db, plan.id, reviewed)

    assert accepted["node"]["state"] == "accepted"
    assert turn_accounting(db, _REVIEW_TURN) == snapshot
    assert _usage_accounting(db) == snapshot


@pytest.mark.parametrize("turn_state", ["missing", "completed"])
def test_accept_succeeds_without_attribution_when_reviewer_turn_is_not_live(
    db, turn_state,
):
    plan = service.ensure_plan("thread", "owner", db=db)
    _task, reviewed = _in_review(db, plan.id)
    if turn_state == "completed":
        open_turn(db, "owner", _REVIEW_TURN)
        _record_usage(db)
        finish_turn(db, _REVIEW_TURN)

    accepted = _accept(db, plan.id, reviewed)

    assert accepted["node"]["state"] == "accepted"
    assert db.execute(
        "SELECT COUNT(*) FROM active_turns WHERE plan_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM llm_usage WHERE plan_id IS NOT NULL"
    ).fetchone()[0] == 0
