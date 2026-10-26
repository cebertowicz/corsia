"""A node's model cost is Measurement's price for the same ``llm_usage`` rows.

``llm_usage.cost_usd`` is NULL for subscription-harness and External rows, so the
stored sum left a Claude Code or Codex task's dollars unknown. ``node_cost`` prices
the node's rows through ``api.measurement_cost`` (admission, identity and
``classify_usage_rows``); a group Measurement cannot price keeps the stored sum and
counts its unstored rows as unknown, as before.
"""

from __future__ import annotations

import pytest

from api.cost_identity import IdentityResolver
from api.measurement_cost import (
    ADMISSION_SQL, IDLE_HOURS_SQL, INPUT_TIER_SQL, classify_usage_rows, endpoint_listings,
)
from plans import node_cost, service
from tests.test_plan_node_cost import _task, db  # noqa: F401  (db is the fixture)

CLAUDE = "claude-code/claude-opus-5-5"
CODEX = "codex/gpt-5.6-terra"
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
    """Measurement's own read of the node's rows: the grouped shape and ``classify_usage_rows``."""
    rows = [dict(row) for row in db.execute(
        f"""SELECT u.model AS model,{ADMISSION_SQL} AS admission,{INPUT_TIER_SQL} AS input_tier,
                   {IDLE_HOURS_SQL} AS idle_hours,SUM(u.prompt_tokens) AS prompt_tokens,
                   SUM(u.completion_tokens) AS completion_tokens,
                   SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens
            FROM llm_usage u WHERE u.plan_node_id=? GROUP BY u.model,admission,input_tier,idle_hours""",
        (node.id,),
    )]
    for row in rows:
        row["model"] = IdentityResolver(db)(row).key
    classified = classify_usage_rows(rows, endpoint_listings(db))
    assert {row["outcome"] for row in classified} == {"priced"}
    return sum(row["dollars"] for row in classified)


def test_claude_code_and_codex_rows_read_the_dollars_measurement_prices(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Cross-vendor")
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code", cache_read=500)
    _row(db, node, RECEIPT.format(0), CODEX, source="external_harness:codex")
    _row(db, node, RECEIPT.format(1), CODEX, source="external_harness:codex")

    own = node_cost.project_many([plan.id], db=db)[plan.id][node.id]["own"]

    expected = _measurement(db, node)
    assert expected > 0
    assert own["model_cost_usd"]["unknown_rows"] == 0
    assert own["model_cost_usd"]["known"] == pytest.approx(expected)
    assert own["tokens"]["known"] == 3 * 200_000 + 500


def test_a_local_row_stays_a_known_zero(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Local")
    _row(db, node, "u-local", "omlx@server/qwen", source="conversation", cost=0.0)

    own = node_cost.project_many([plan.id], db=db)[plan.id][node.id]["own"]

    assert own["model_cost_usd"] == {"known": 0.0, "unknown_rows": 0}


def test_an_external_row_without_a_listing_stays_unknown(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "External")
    _row(db, node, "u-external", "external@unlisted/model", source="conversation")

    own = node_cost.project_many([plan.id], db=db)[plan.id][node.id]["own"]

    assert own["model_cost_usd"] == {"known": None, "unknown_rows": 1}


def test_a_codex_row_admission_refuses_is_counted_unknown_not_priced(db):
    """A hyphenated id is not a 24-hex receipt: the row predates the proven token contract."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Before the contract")
    other = _task(db, plan.id, "Claude alone")
    _row(db, node, "legacy-codex-row", CODEX, source="external_harness:codex", cache_read=500)
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code")
    _row(db, other, "u-claude-alone", CLAUDE, source="external_harness:claude-code")

    costs = node_cost.project_many([plan.id], db=db)[plan.id]

    assert costs[node.id]["own"]["model_cost_usd"]["unknown_rows"] == 1
    assert costs[node.id]["own"]["model_cost_usd"]["known"] == pytest.approx(_measurement(db, other))


def test_the_commitment_total_reads_the_same_dollars_as_its_node(db):
    from plans import commitment_cost

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Cross-vendor")
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code")
    _row(db, node, RECEIPT.format(0), CODEX, source="external_harness:codex")

    rolled = node_cost.project_many([plan.id], db=db)[plan.id][node.id]["rolled"]
    total = commitment_cost.project(node.id, db=db)

    assert rolled["model_cost_usd"]["known"] == pytest.approx(_measurement(db, node))
    assert total["model_cost_usd"] == rolled["model_cost_usd"]


def test_a_stored_cost_is_kept_and_only_unstored_rows_are_priced(db):
    """A recorded charge is never repriced; the node adds Measurement's dollars for the rest."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Mixed")
    other = _task(db, plan.id, "Claude alone")
    _row(db, node, "u-cloud", "openai/gpt-5", source="conversation", cost=1.25)
    _row(db, node, "u-claude", CLAUDE, source="external_harness:claude-code")
    _row(db, other, "u-claude-alone", CLAUDE, source="external_harness:claude-code")

    own = node_cost.project_many([plan.id], db=db)[plan.id][node.id]["own"]

    assert own["model_cost_usd"]["unknown_rows"] == 0
    assert own["model_cost_usd"]["known"] == pytest.approx(1.25 + _measurement(db, other))
