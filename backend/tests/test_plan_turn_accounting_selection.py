"""A Turn that selects a task mid-Turn carries its whole cost (#579).

`admission_attempts.bind_turn` binds a still-unbound running Turn to the
attempt a plan mutation selects. Usage the Turn recorded before that selection,
both projected `llm_usage` rows and pending receipts, is backfilled onto the
same attempt. A Turn already bound keeps its first attempt and its usage.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from infra.llm_usage import record_llm_usage
from infra.schema import SCHEMA_SQL
from infra.turn_usage_receipts import store_turn_usage_receipt
from plans import service
from plans.boundary import PlanActor, mutate
from tests.plan_execution_helpers import add_task, open_turn, turn_accounting

_TURN = "selecting-turn"
_PLAN_COLUMNS = (
    "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role"
)
_UNBOUND = (None, None, None, None, None)


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


def _activate(db, plan_id, task, key) -> dict:
    return mutate(
        plan_id,
        "activate",
        {"node_id": task.id, "expected_revision": task.revision},
        actor=PlanActor("owner", "user", None, turn_id=_TURN),
        provenance="test.turn_accounting_selection",
        mutation_id=key,
        db=db,
    ).result


def _no_event(*_args, **_kwargs) -> None:
    return None


def _record_usage(db, key: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("infra.database.get_db", lambda: _KeepOpen(db))
        record_llm_usage(
            "owner",
            "test-model",
            {"total_tokens": 9},
            turn_id=_TURN,
            usage_key=key,
            _emit_product_event=_no_event,
        )


def _usage_rows(db) -> list[tuple]:
    rows = db.execute(
        f"SELECT {_PLAN_COLUMNS} FROM llm_usage WHERE turn_id=? ORDER BY id",
        (_TURN,),
    ).fetchall()
    return [tuple(row) for row in rows]


def _receipt_bindings(db) -> list[tuple]:
    raw = db.execute(
        "SELECT usage_receipts_json FROM active_turns WHERE id=?", (_TURN,),
    ).fetchone()["usage_receipts_json"]
    return [
        tuple(payload["binding"][field] for field in _PLAN_COLUMNS.split(","))
        for payload in json.loads(raw).values()
    ]


def _expected(db, plan_id, task) -> tuple:
    attempt = db.execute(
        """SELECT id FROM plan_node_attempts
           WHERE node_id=? ORDER BY attempt_ordinal DESC LIMIT 1""",
        (task.id,),
    ).fetchone()["id"]
    return (plan_id, task.id, attempt, task.commitment_id, "execution")


def test_mid_turn_selection_backfills_rows_recorded_before_it(db):
    """Every row of a Turn that selects after its first call goes to the task."""
    plan = service.ensure_plan("thread", "owner", db=db)
    task = add_task(db, plan.id, "Selected mid-Turn")
    open_turn(db, "owner", _TURN)
    _record_usage(db, "provider-call:1")
    _record_usage(db, "provider-call:2")
    assert _usage_rows(db) == [_UNBOUND, _UNBOUND]

    _activate(db, plan.id, task, "activate-mid-turn")
    _record_usage(db, "provider-call:3")

    expected = _expected(db, plan.id, task)
    assert turn_accounting(db, _TURN) == expected
    assert _usage_rows(db) == [expected, expected, expected]


def test_mid_turn_selection_backfills_pending_receipts(db):
    """A receipt stored before the selection, not yet projected, is rebound."""
    plan = service.ensure_plan("thread", "owner", db=db)
    task = add_task(db, plan.id, "Selected with a receipt pending")
    open_turn(db, "owner", _TURN)
    stored = store_turn_usage_receipt(
        db,
        thread_id="owner",
        turn_id=_TURN,
        usage_key="provider-call:pending",
        model="test-model",
        measured={"total_tokens": 4},
        ttft_ms=None,
        source="conversation",
        now_iso="t",
    )
    assert stored is not None
    assert _receipt_bindings(db) == [_UNBOUND]

    _activate(db, plan.id, task, "activate-with-receipt")

    assert _receipt_bindings(db) == [_expected(db, plan.id, task)]


def test_already_bound_turn_is_not_rewritten_by_a_later_selection(db):
    """The first attempt a Turn carries stays; a later selection moves nothing."""
    plan = service.ensure_plan("thread", "owner", db=db)
    first = add_task(db, plan.id, "First selection")
    second = add_task(db, plan.id, "Later selection")
    open_turn(db, "owner", _TURN)
    _activate(db, plan.id, first, "activate-first")
    _record_usage(db, "provider-call:1")
    snapshot = turn_accounting(db, _TURN)
    assert snapshot == _expected(db, plan.id, first)
    mutate(
        plan.id,
        "close_node",
        {
            "node_id": first.id,
            "reason": "Not needed after all",
            "expected_revision": service._node_row(db, first.id)["revision"],
        },
        actor=PlanActor("owner", "user", None, turn_id=_TURN),
        provenance="test.turn_accounting_selection",
        mutation_id="close-first",
        db=db,
    )
    _activate(db, plan.id, _fresh(db, second), "activate-second")
    _record_usage(db, "provider-call:2")

    assert service._node_row(db, second.id)["state"] == "active"
    assert turn_accounting(db, _TURN) == snapshot
    assert _usage_rows(db) == [snapshot, snapshot]


def _fresh(db, task):
    """The task with its current revision, for a second mutation on it."""
    row = service._node_row(db, task.id)
    return type(task)(**{**task.__dict__, "revision": row["revision"]})
