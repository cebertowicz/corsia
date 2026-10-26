"""A plan's model cost is Measurement's API-equivalent price of every usage row.

``usage_cost.usage_dollars`` prices a node's or a commitment's rows through
``core.measurement_pricing`` (the grouped read, admission and ``classify_usage_rows``
Measurement's Cost and Models read) and never reads the stored ``cost_usd``. A local
model's rows have no API price: they add nothing and are not unknown. A row
Measurement cannot price counts unknown.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.cost_identity import IdentityResolver
from core.measurement_pricing import classify_usage_rows, endpoint_listings, grouped_usage_sql
from plans import commitment_cost, node_cost, operator_node_facts, service, usage_cost
from tests.test_plan_node_cost import _task, db  # noqa: F401  (db is the fixture)

CLAUDE = "claude-code/claude-opus-5-5"
CODEX = "codex/gpt-5.6-terra"
LOCAL = "omlx@server/qwen"
RECEIPT = "0123456789abcdef0123456{}"  # 24 hex: the Codex writer whose token contract is proven


def _row(db, node, usage_id, model, *, source, prompt=100_000, completion=100_000, cache_read=0,
         cost=None, created_at="2026-09-29T12:00:00Z"):
    total = prompt + completion + cache_read
    db.execute(
        """INSERT INTO llm_usage (
               id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,
               cache_read_tokens,total_tokens,cost_usd,source,created_at,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role
           ) VALUES (?,'owner',?,?,?,0,?,?,?,?,?,?,?,?,?,'execution')""",
        (usage_id, model, prompt, completion, cache_read, total, cost, source, created_at,
         node.plan_id, node.id, f"attempt-{usage_id}", node.commitment_id),
    )
    db.commit()


def _measurement(db, node) -> float:
    """Measurement's price of the node's rows, every one of which it prices, from the shared builder."""
    rows = [dict(row) for row in db.execute(
        grouped_usage_sql({"node": "u.plan_node_id"}, "llm_usage u WHERE u.plan_node_id=?"), (node.id,),
    )]
    resolve = IdentityResolver(db)
    for row in rows:
        row["model"] = resolve(row).key
    classified = classify_usage_rows(rows, endpoint_listings(db))
    assert {row["outcome"] for row in classified} == {"priced"}
    return sum(row["dollars"] for row in classified)


def _own(db, plan, node) -> dict:
    return node_cost.project_many([plan.id], db=db)[plan.id][node.id]["own"]


def test_claude_code_and_codex_rows_read_the_dollars_measurement_prices(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Cross-vendor")
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code", cache_read=500)
    _row(db, node, RECEIPT.format(0), CODEX, source="external_harness:codex")
    _row(db, node, RECEIPT.format(1), CODEX, source="external_harness:codex")

    own = _own(db, plan, node)

    expected = _measurement(db, node)
    assert expected > 0
    assert own["model_cost_usd"] == {"known": pytest.approx(expected), "unknown_rows": 0}
    assert own["tokens"]["known"] == 3 * 200_000 + 500


def test_a_stored_cost_is_not_read_and_the_row_is_priced_like_measurement(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Stored")
    other = _task(db, plan.id, "Unstored")
    _row(db, node, "u-stored", CLAUDE, source="external_harness:claude-code", cost=9.99)
    _row(db, other, "u-unstored", CLAUDE, source="external_harness:claude-code")

    own = _own(db, plan, node)

    assert own["model_cost_usd"] == {"known": pytest.approx(_measurement(db, other)), "unknown_rows": 0}
    assert own["model_cost_usd"]["known"] == pytest.approx(_measurement(db, node))


def test_local_rows_add_no_dollars_and_are_not_unknown(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Local")
    _row(db, node, "u-local", LOCAL, source="conversation", cost=0.0)

    assert _own(db, plan, node)["model_cost_usd"] == {"known": 0, "unknown_rows": 0}
    assert usage_cost.usage_dollars(db, {"plan_node_id": [node.id]})["plan_node_id"] == {
        node.id: {"known": None, "unknown_rows": 0},
    }


def test_claude_code_with_local_rows_is_the_claude_code_price_and_nothing_unknown(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Mixed")
    claude_only = _task(db, plan.id, "Claude alone")
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code")
    _row(db, node, "u-local-1", LOCAL, source="conversation")
    _row(db, node, "u-local-2", LOCAL, source="conversation", cost=0.0)
    _row(db, claude_only, "u-claude-alone", CLAUDE, source="external_harness:claude-code")

    own = _own(db, plan, node)

    assert own["model_cost_usd"] == {"known": pytest.approx(_measurement(db, claude_only)), "unknown_rows": 0}
    assert own["tokens"] == {"known": 3 * 200_000, "unknown_rows": 0}


def test_an_external_row_without_a_listing_has_no_api_price_as_in_measurement(db):
    """Measurement classes an External endpoint with no public list rate ``not_api_priced``, like a local model."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "External")
    _row(db, node, "u-external", "external@unlisted/model", source="conversation")

    assert usage_cost.usage_dollars(db, {"plan_node_id": [node.id]})["plan_node_id"] == {
        node.id: {"known": None, "unknown_rows": 0},
    }


def test_a_claude_code_row_with_no_list_price_is_unknown(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Unlisted")
    _row(db, node, "u-unlisted", "claude-code/claude-unlisted-9", source="external_harness:claude-code")

    assert _own(db, plan, node)["model_cost_usd"] == {"known": None, "unknown_rows": 1}


def test_a_codex_row_admission_refuses_is_counted_unknown_not_priced(db):
    """A hyphenated id is not a 24-hex receipt: the row predates the proven token contract."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Before the contract")
    other = _task(db, plan.id, "Claude alone")
    _row(db, node, "legacy-codex-row", CODEX, source="external_harness:codex", cache_read=500)
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code")
    _row(db, other, "u-claude-alone", CLAUDE, source="external_harness:claude-code")

    own = _own(db, plan, node)

    assert own["model_cost_usd"] == {"known": pytest.approx(_measurement(db, other)), "unknown_rows": 1}


def test_the_commitment_total_reads_the_same_dollars_as_its_node(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Cross-vendor")
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code", cost=3.0)
    _row(db, node, RECEIPT.format(0), CODEX, source="external_harness:codex")
    _row(db, node, "u-local", LOCAL, source="conversation", cost=0.0)
    _row(db, node, "legacy-codex-row", CODEX, source="external_harness:codex", cache_read=500)

    rolled = node_cost.project_many([plan.id], db=db)[plan.id][node.id]["rolled"]
    total = commitment_cost.project(node.id, db=db)

    assert rolled["model_cost_usd"]["unknown_rows"] == 1
    assert total["model_cost_usd"] == rolled["model_cost_usd"]
    assert total["tokens"] == rolled["tokens"]


def test_one_priced_read_gives_the_nodes_and_commitments_their_own_dollars(db, monkeypatch):
    """The operator read prices the ledger once and passes the commitments' dollars on.

    A commitment's row on a node outside the projected plans still counts toward it: the
    one read covers every row either id list names.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Projected")
    elsewhere = _task(db, service.ensure_plan("thread", "other", db=db).id, "Outside the projection")
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code")
    _row(db, node, "legacy-codex-row", CODEX, source="external_harness:codex", cache_read=500)
    _row(db, elsewhere, RECEIPT.format(0), CODEX, source="external_harness:codex")
    db.execute("UPDATE llm_usage SET plan_commitment_id=? WHERE id=?", (node.commitment_id, RECEIPT.format(0)))
    db.commit()
    separate_nodes = node_cost.project_many([plan.id], db=db)
    separate = commitment_cost.project_many([node.commitment_id], db=db)

    real = usage_cost.usage_dollars
    reads: list[dict] = []
    monkeypatch.setattr(usage_cost, "usage_dollars", lambda *args: reads.append(args[1]) or real(*args))
    nodes, _initiatives, dollars = node_cost.project_with_commitments([plan.id], [node.commitment_id], db=db)
    shared = commitment_cost.project_many([node.commitment_id], db=db, dollars=dollars)

    assert len(reads) == 1
    assert nodes == separate_nodes
    assert shared == separate
    assert shared[node.commitment_id]["model_cost_usd"]["unknown_rows"] == 1
    assert shared[node.commitment_id]["model_cost_usd"]["known"] > nodes[plan.id][node.id]["own"]["model_cost_usd"]["known"]


def test_the_open_work_read_prices_its_nodes_and_commitments_in_one_read(db, monkeypatch):
    """The scoped operator read (``detail_ids``) prices the listed nodes and the commitments they root once."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Listed")
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code")
    _row(db, node, "legacy-codex-row", CODEX, source="external_harness:codex", cache_read=500)
    node_plans = [({"id": node.id, "commitment_id": node.commitment_id}, {"id": plan.id})]
    separate = commitment_cost.project_many([node.commitment_id], db=db)
    separate_own = _own(db, plan, node)

    real = usage_cost.usage_dollars
    reads: list[dict] = []
    monkeypatch.setattr(usage_cost, "usage_dollars", lambda *args: reads.append(args[1]) or real(*args))
    commitments, nodes, _initiatives = operator_node_facts.project_costs(
        node_plans, db=db, now=datetime.now(timezone.utc), detail_ids=frozenset({node.id}),
    )

    assert len(reads) == 1
    assert commitments == separate
    assert nodes[plan.id][node.id]["own"] == separate_own


def test_the_pricing_queries_do_not_grow_with_the_rows(db):
    """Rows are grouped in SQL: the query count follows the distinct shapes, never the rows."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Many calls")

    def queries() -> int:
        seen: list[str] = []
        db.set_trace_callback(seen.append)
        try:
            own = _own(db, plan, node)
            commitment_cost.project_many([node.id], db=db)
        finally:
            db.set_trace_callback(None)
        assert own["model_cost_usd"]["unknown_rows"] == 0
        return len(seen)

    for number in range(3):
        _row(db, node, f"u-claude-{number}", CLAUDE, source="external_harness:claude-code")
    few = queries()
    for number in range(3, 60):
        _row(db, node, f"u-claude-{number}", CLAUDE, source="external_harness:claude-code")

    assert queries() == few
