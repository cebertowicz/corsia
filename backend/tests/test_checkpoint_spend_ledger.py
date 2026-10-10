"""Checkpoint receipts are atomic, frozen-attributed and independent of Turns."""

import json

import pytest

from infra import checkpoint_queries, checkpoints
from infra.provider_usage import TokenUsage
from infra.turn_usage_receipts import PLAN_FIELDS, UsageReceiptConflictError
from tests.test_checkpoint_store import _db, _request, _seed_thread


@pytest.fixture
def checkpoint_db():
    db = _db()
    _seed_thread(db, "thread-1")
    checkpoints.create_running(_request("thread-1", "cp-1"), db=db)
    yield db
    db.close()


def _binding():
    return {field: None for field in PLAN_FIELDS}


def _usage():
    return {**TokenUsage(input_uncached=100, cache_read=300, output=50).ledger_row(), "cost_usd": None}


def test_receipt_and_ledger_commit_once_without_foreground_turn(checkpoint_db):
    from infra.checkpoint_usage import freeze_binding, record_call

    db = checkpoint_db
    freeze_binding("cp-1", _binding(), db=db)
    for _ in range(2):
        record_call("cp-1", "call-1", "native/test", _usage(), now_iso="t2", db=db)
    rows = db.execute("SELECT * FROM llm_usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["turn_id"] is None
    assert rows[0]["total_tokens"] == 450
    assert rows[0]["cost_usd"] is None
    assert rows[0]["source"] == "checkpoint"
    payload = json.loads(checkpoint_queries.read("cp-1", db=db).usage_json)
    assert len(payload["receipts"]) == 1
    assert payload["originating_turn_id"] == "turn-1"
    assert payload["calls"][0]["total_tokens"] == 450


def test_changed_measurement_conflicts_instead_of_double_charging(checkpoint_db):
    from infra.checkpoint_usage import freeze_binding, record_call

    db = checkpoint_db
    freeze_binding("cp-1", _binding(), db=db)
    record_call("cp-1", "call-1", "native/test", _usage(), now_iso="t2", db=db)
    with pytest.raises(UsageReceiptConflictError):
        record_call("cp-1", "call-1", "native/test", {**_usage(), "completion_tokens": 51}, now_iso="t3", db=db)
    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1


@pytest.mark.parametrize("ending", ["capture", "fail", "cancel"])
def test_terminal_transition_preserves_already_paid_calls(checkpoint_db, ending):
    from infra.checkpoint_usage import freeze_binding, record_call

    db = checkpoint_db
    freeze_binding("cp-1", _binding(), db=db)
    record_call("cp-1", "call-1", "native/test", _usage(), now_iso="t2", db=db)
    before = checkpoint_queries.read("cp-1", db=db).usage_json
    if ending == "capture":
        checkpoints.capture("cp-1", raw_result="done", usage_json='{"calls":[]}', captured_at="t3", db=db)
    elif ending == "fail":
        checkpoints.fail("cp-1", error="provider failed", ended_at="t3", db=db)
    else:
        checkpoints.cancel("cp-1", ended_at="t3", db=db)
    assert checkpoint_queries.read("cp-1", db=db).usage_json == before


def test_ledger_failure_rolls_back_receipt(checkpoint_db, monkeypatch):
    from infra import checkpoint_usage

    db = checkpoint_db
    checkpoint_usage.freeze_binding("cp-1", _binding(), db=db)
    before = checkpoint_queries.read("cp-1", db=db).usage_json

    def broken(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(checkpoint_usage, "write_usage_row", broken)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        checkpoint_usage.record_call("cp-1", "call-1", "native/test", _usage(), now_iso="t2", db=db)
    assert checkpoint_queries.read("cp-1", db=db).usage_json == before


def test_frozen_binding_cannot_be_reassigned(checkpoint_db):
    from infra.checkpoint_usage import freeze_binding

    db = checkpoint_db
    freeze_binding("cp-1", _binding(), db=db)
    with pytest.raises(UsageReceiptConflictError):
        freeze_binding("cp-1", {**_binding(), "plan_node_id": "later-task"}, db=db)


def test_late_receipt_survives_cancel_and_restart_read(checkpoint_db):
    from infra.checkpoint_usage import freeze_binding, record_call

    db = checkpoint_db
    freeze_binding("cp-1", _binding(), db=db)
    checkpoints.cancel("cp-1", ended_at="t2", db=db)
    record_call("cp-1", "call-1", "native/test", _usage(), now_iso="t3", db=db)
    assert checkpoint_queries.read("cp-1", db=db).status == "cancelled"
    assert db.execute("SELECT total_tokens FROM llm_usage").fetchone()[0] == 450
