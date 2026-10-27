"""A task's dollars equal what Measurement's Cost read serves for the same usage rows.

The oracle is the figure Measurement actually serves: ``measurement_cost.build_cost`` over
its scope, reading the hourly usage rollup (``usage_rollup``), which groups by hour, thread
and account before pricing. The plan read groups the same rows by node instead. Pricing is
per token, so the two groupings must give one figure, and the same rows unpriced.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.test_measurement_cost import PARAMS, _seed
from tests.test_plan_node_cost_price import CLAUDE, CODEX, RECEIPT, _row

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
# A local model, which both reads class as having no API price.
LOCAL = "omlx@local/qwen"


def test_a_tasks_dollars_equal_the_cost_measurement_serves_from_the_rollup(isolated_db):
    from api.measurement_cost import build_cost
    from api.usage_rollup import refresh_step, rollup_through, usage_params
    from infra.database import get_db
    from plans import commitment_cost, node_cost, service

    db = get_db()
    try:
        _seed(db, [])
        db.execute("DELETE FROM llm_usage WHERE thread_id='elsewhere'")
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('thread','steve','t','t','t')")
        plan = service.ensure_plan("thread", "thread", db=db)
        node = service.add_node(plan.id, kind="task", title="Task", acceptance_criteria="d", db=db)
        # Calls spread over days and hours of one week, so the rollup holds several groups per model.
        calls = [
            ("u-claude-1", CLAUDE, "external_harness:claude-code", 120_000, 4_000, "2026-09-21T09:15:00Z"),
            ("u-claude-2", CLAUDE, "external_harness:claude-code", 30_000, 70_000, "2026-09-21T09:45:00Z"),
            ("u-claude-3", CLAUDE, "external_harness:claude-code", 1_000, 900_000, "2026-09-24T17:05:00Z"),
            (RECEIPT.format(0), CODEX, "external_harness:codex", 200_000, 0, "2026-09-22T03:00:00Z"),
            (RECEIPT.format(1), CODEX, "external_harness:codex", 50_000, 120_000, "2026-09-26T23:59:00Z"),
            ("u-local", LOCAL, "conversation", 9_000, 0, "2026-09-23T12:00:00Z"),
            ("legacy-codex-row", CODEX, "external_harness:codex", 10, 500, "2026-09-25T08:00:00Z"),
        ]
        for usage_id, model, source, prompt, cache_read, created_at in calls:
            _row(db, node, usage_id, model, source=source, prompt=prompt, cache_read=cache_read,
                 created_at=created_at)
        db.execute("UPDATE llm_usage SET thread_id='thread' WHERE plan_node_id=?", (node.id,))
        db.commit()
        while refresh_step(db, NOW):
            pass
        assert usage_params(db, PARAMS)["rollup_through"] == rollup_through(db) >= "2026-09-28"
        assert db.execute("SELECT COUNT(*) FROM llm_usage_hourly").fetchone()[0] >= 6
        served = build_cost(db, PARAMS)
        own = node_cost.project_many([plan.id], db=db, now=NOW)[plan.id][node.id]["own"]["model_cost_usd"]
        commitment = commitment_cost.project(node.commitment_id, db=db, now=NOW)["model_cost_usd"]
    finally:
        db.close()

    excluded = sum(item["records"] for item in served["excluded"])
    assert served["latest"]["dollars"] > 0
    assert (served["coverage"]["priced_records"], excluded, served["not_api_priced_records"]) == (5, 1, 1)
    # Measurement serves dollars rounded to 1e-10; the plan sums the unrounded group prices.
    assert own == {"known": pytest.approx(served["latest"]["dollars"], abs=1e-9), "unknown_rows": excluded}
    assert commitment == own
