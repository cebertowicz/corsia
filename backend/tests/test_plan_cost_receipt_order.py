"""A call committing while a plan cost read runs is counted unknown, never silently missing.

The projection writing a call's ledger row deletes its pending receipt in the same
transaction. A commitment's figures read what the ledger still owes (its receipts) no later
than what it holds (the priced rows), so a call landing between the two reads is briefly
counted unknown by a row that covers it. Read the other way round, it is in neither read and
the commitment's dollars are short with nothing unknown. Each operator read is checked with
the projection committed between its first and second read.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from infra import turn_usage_remainder
from infra.turn_usage_receipts import project_turn_usage_receipt, store_turn_usage_receipt
from plans import commitment_cost, operator_node_facts, service, usage_cost
from plans.boundary import PlanActor, mutate
from tests.plan_execution_helpers import open_turn
from tests.test_plan_node_cost import _task, db  # noqa: F401  (db is the fixture)
from tests.test_plan_node_cost_price import CLAUDE

TURN = "turn-receipt-order"


def _pending_call(db) -> tuple[dict, str]:
    """A selected task whose Turn holds one priced call's receipt, not yet projected."""
    plan = service.ensure_plan("thread", "owner", db=db)
    task = _task(db, plan.id, "Selected")
    open_turn(db, "owner", TURN)
    mutate(
        plan.id, "activate", {"node_id": task.id, "expected_revision": task.revision},
        actor=PlanActor("owner", "user-1", "project-1", turn_id=TURN),
        provenance="test.receipt_order", mutation_id="activate-receipt-order", db=db,
    )
    stored = store_turn_usage_receipt(
        db, thread_id="owner", turn_id=TURN, usage_key="provider-call:1", model=CLAUDE,
        measured={
            "prompt_tokens": 100_000, "completion_tokens": 100_000, "cache_creation_tokens": 0,
            "cache_read_tokens": 0, "total_tokens": 200_000,
        },
        ttft_ms=None, source="external_harness:claude-code", now_iso="2026-09-29T12:00:00Z",
    )
    assert stored is not None and stored[2]["plan_commitment_id"] == task.commitment_id
    db.commit()
    return {"id": task.id, "commitment_id": task.commitment_id}, stored[0]


def _commit_between_reads(db, monkeypatch, receipt: str) -> None:
    """Project the receipt once, right after whichever of the two reads runs first."""
    done: list[bool] = []

    def after(read):
        def wrapped(*args, **kwargs):
            result = read(*args, **kwargs)
            if not done:
                done.append(True)
                assert project_turn_usage_receipt(db, TURN, receipt) is not None
            return result
        return wrapped

    monkeypatch.setattr(usage_cost, "usage_dollars", after(usage_cost.usage_dollars))
    for module in (turn_usage_remainder, usage_cost, commitment_cost):
        if hasattr(module, "pending_measurement_counts"):
            monkeypatch.setattr(module, "pending_measurement_counts", after(module.pending_measurement_counts))


@pytest.mark.parametrize("scoped", [False, True], ids=["full read", "open-work read"])
def test_a_call_projected_between_the_reads_counts_unknown(db, monkeypatch, scoped):
    node, receipt = _pending_call(db)
    plan = {"id": service.get_node(node["id"], db=db).plan_id}
    _commit_between_reads(db, monkeypatch, receipt)

    costs, _nodes, _initiatives = operator_node_facts.project_costs(
        [(node, plan)], db=db, now=datetime.now(timezone.utc),
        detail_ids=frozenset({node["id"]}) if scoped else None,
    )

    assert db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1
    dollars = costs[node["commitment_id"]]["model_cost_usd"]
    # Briefly both: the receipt read first still owes the call, and the row read after prices it.
    assert dollars["unknown_rows"] == 1 and dollars["known"] > 0, dollars


@pytest.mark.parametrize("scoped", [False, True], ids=["full read", "open-work read"])
def test_a_pending_call_counts_unknown_and_its_row_once_projected_counts_priced(db, scoped):
    node, receipt = _pending_call(db)
    plan = {"id": service.get_node(node["id"], db=db).plan_id}

    def read() -> dict:
        costs, _nodes, _initiatives = operator_node_facts.project_costs(
            [(node, plan)], db=db, now=datetime.now(timezone.utc),
            detail_ids=frozenset({node["id"]}) if scoped else None,
        )
        return costs[node["commitment_id"]]["model_cost_usd"]

    assert read() == {"known": None, "unknown_rows": 1}
    project_turn_usage_receipt(db, TURN, receipt)
    projected = read()
    assert projected["unknown_rows"] == 0 and projected["known"] > 0
