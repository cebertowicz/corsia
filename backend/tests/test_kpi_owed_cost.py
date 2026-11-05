"""Thread cost is incomplete while its ledger owes measurements."""
from __future__ import annotations

import pytest

from core.kpi_thread_cost import thread_costs
from infra.retained_usage import HeldUsage, hold_usage
from infra.turn_usage_receipts import store_turn_usage_receipt
from tests.test_kpi_cost_of import ledger, open_turn, usage  # noqa: F401

CALL = {"prompt_tokens": 10, "completion_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "total_tokens": 10}


def receipt(db, thread, turn):
    return store_turn_usage_receipt(db, thread_id=thread, turn_id=turn, usage_key="call",
                                    model="claude-code/opus", measured=CALL, ttft_ms=None,
                                    source="external_harness:claude-code", now_iso="2026-01-02T00:00:00Z")


def test_pending_receipt_makes_cost_unknown(ledger):
    open_turn(ledger, "pending", "w1", "running")
    assert receipt(ledger, "w1", "pending")
    assert thread_costs(ledger, "steve", ["w1"]) == {"w1": None}


def test_held_call_makes_cost_unknown(ledger, monkeypatch):
    from infra import retained_usage

    monkeypatch.setattr(retained_usage, "_held", {})
    open_turn(ledger, "held", "w1", "running")
    hold_usage(HeldUsage(thread_id="w1", turn_id="held", usage_key="call", usage_id="held-id",
                         model="claude-code/opus", measured=CALL, ttft_ms=None, binding=None,
                         source="external_harness:claude-code", now_iso="2026-01-02T00:00:00Z"))
    assert thread_costs(ledger, "steve", ["w1"]) == {"w1": None}
    from infra.turn_usage_remainder import pending_thread_measurement_counts
    assert pending_thread_measurement_counts(ledger, ["w1"])["w1"]["held"] == 1
    assert receipt(ledger, "w1", "held")
    assert pending_thread_measurement_counts(ledger, ["w1"])["w1"]["held"] == 1
    from infra.turn_usage_receipts import project_turn_usage_receipt
    assert project_turn_usage_receipt(ledger, "held", receipt(ledger, "w1", "held")[0])
    assert pending_thread_measurement_counts(ledger, ["w1"]) == {}


@pytest.mark.parametrize("status", ["completed", "error"])
def test_settled_unmeasured_turn_makes_cost_unknown(ledger, status):
    open_turn(ledger, "missing", "w1", status)
    assert thread_costs(ledger, "steve", ["w1"]) == {"w1": None}


def test_measured_turn_is_known_alongside_unmeasured_thread(ledger):
    open_turn(ledger, "measured", "w1")
    usage(ledger, "measured-row", "w1", turn="measured")
    open_turn(ledger, "unmeasured", "w2")
    costs = thread_costs(ledger, "steve", ["w1", "w2"])
    assert costs["w1"] > 0 and costs["w2"] is None


def test_receipt_projected_between_reads_remains_unknown(ledger, monkeypatch):
    from core import kpi_thread_cost
    from infra.turn_usage_receipts import project_turn_usage_receipt

    open_turn(ledger, "race", "w1", "running")
    stored = receipt(ledger, "w1", "race")
    ledger.commit()
    original = kpi_thread_cost.thread_measurement_remainder

    def project_after_read(db, ids):
        result = original(db, ids)
        assert project_turn_usage_receipt(db, "race", stored[0]) is not None
        return result

    monkeypatch.setattr(kpi_thread_cost, "thread_measurement_remainder", project_after_read)
    assert thread_costs(ledger, "steve", ["w1"]) == {"w1": None}


def test_remainder_queries_use_bounded_indexes(ledger):
    from infra.turn_usage_remainder import thread_measurement_remainder

    queries = []

    class Recording:
        def execute(self, sql, params=()):
            queries.append((sql, params))
            return ledger.execute(sql, params)

    thread_measurement_remainder(Recording(), ["w1"])
    plans = [[row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + sql, params)]
             for sql, params in queries]
    print("REMAINDER QUERY PLANS:", plans)
    assert any("idx_active_turns_pending_usage" in part for part in plans[0])
    assert any("SEARCH active_turns" in part and "thread_id=?" in part for part in plans[1])
    statements = []
    ledger.set_trace_callback(statements.append)
    thread_costs(ledger, "steve", ["w1"])
    ledger.set_trace_callback(None)
    sql = next(sql for sql in statements if "FROM llm_usage u WHERE" in sql and "LIMIT 0" not in sql)
    print("PRICING QUERY PLAN:", [row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + sql)])
    held_sql = "SELECT usage_receipts_json,plan_commitment_id,thread_id FROM active_turns WHERE id=?"
    print("HELD TURN QUERY PLAN:", [row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + held_sql, ("turn",))])
