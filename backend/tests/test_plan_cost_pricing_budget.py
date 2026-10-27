"""A plan cost read prices the usage ledger once, however many plans it projects.

The open-work read (``detail_ids``) and the full operator read each price every node they
sum (listed nodes, the unlisted nodes beneath them, the plans attached beneath those and the
rows an initiative total reads from outside them) and the commitments they show in one
``usage_cost.usage_dollars`` read. A plan in scope adds rows to that read, never a read.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from plans import followups, mutations, operator_node_facts, service, usage_cost
from tests.test_plan_node_cost import _task, db  # noqa: F401  (db is the fixture)
from tests.test_plan_node_cost_price import CLAUDE, _row

SOURCE = "external_harness:claude-code"


def _lead(db, index: int) -> list[tuple[dict, dict]]:
    """One lead's plan: a phase over an unlisted step, a delegated step and a follow-up lineage.

    Returns the open-work read's listed rows: the phase and the lineage's first member.
    """
    lead, worker = f"lead-{index}", f"worker-{index}"
    for thread_id, spawned_by in ((lead, None), (worker, lead)):
        db.execute(
            """INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    plan = service.ensure_plan("thread", lead, db=db)
    phase = service.add_node(plan.id, kind="phase", title="Phase", acceptance_criteria="d", db=db)
    unlisted = _task(db, plan.id, "Unlisted step", parent_id=phase.id)
    delegate = _task(db, plan.id, "Delegated", parent_id=phase.id, spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    child_plan = service.ensure_plan("thread", worker, db=db)
    mutations.attach_spawned_thread(delegate.id, worker, db=db)
    inner = _task(db, child_plan.id, "Worker step")
    member = _task(db, plan.id, "Member")
    follow_up = followups.add_follow_up(member.id, title="Follow-up", acceptance_criteria="d", db=db)
    for number, node in enumerate((unlisted, inner, member, follow_up)):
        _row(db, node, f"u-{index}-{number}", CLAUDE, source=SOURCE)
    return [
        ({"id": node.id, "commitment_id": node.commitment_id}, {"id": plan.id})
        for node in (phase, member)
    ]


def _reads(db, monkeypatch, node_plans, *, scoped: bool) -> tuple[int, int, dict]:
    """The ``usage_dollars`` calls and ``llm_usage`` statements one read makes, and its node figures."""
    real = usage_cost.usage_dollars
    calls: list[dict] = []

    def recording(*args):
        calls.append(args[1])
        return real(*args)

    monkeypatch.setattr(usage_cost, "usage_dollars", recording)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        detail = frozenset(str(node["id"]) for node, _plan in node_plans) if scoped else None
        _commitments, nodes, _initiatives = operator_node_facts.project_costs(
            node_plans, db=db, now=datetime.now(timezone.utc), detail_ids=detail,
        )
    finally:
        db.set_trace_callback(None)
        monkeypatch.setattr(usage_cost, "usage_dollars", real)
    return len(calls), sum("llm_usage" in statement for statement in statements), nodes


@pytest.mark.parametrize("scoped", [True, False], ids=["open-work", "full"])
def test_the_pricing_reads_do_not_grow_with_the_plans_in_scope(db, monkeypatch, scoped):
    few = [row for index in range(2) for row in _lead(db, index)]
    few_reads = _reads(db, monkeypatch, few, scoped=scoped)
    many = few + [row for index in range(2, 6) for row in _lead(db, index)]
    many_reads = _reads(db, monkeypatch, many, scoped=scoped)

    assert few_reads[0] == many_reads[0] == 1
    assert few_reads[1] == many_reads[1]
    # Each phase rolls its unlisted step and its worker's plan: twice the member's one call.
    for phase, member in zip(many[::2], many[1::2]):
        plan_figures = many_reads[2][phase[1]["id"]]
        one_call = plan_figures[member[0]["id"]]["own"]["model_cost_usd"]
        assert one_call["known"] > 0 and one_call["unknown_rows"] == 0
        assert plan_figures[phase[0]["id"]]["rolled"]["model_cost_usd"] == {
            "known": pytest.approx(2 * one_call["known"]), "unknown_rows": 0,
        }


def test_a_dollars_map_missing_a_priced_id_fails_rather_than_reading_no_price(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Priced elsewhere")
    _row(db, node, "u-claude", CLAUDE, source=SOURCE)
    from plans import commitment_cost, node_cost

    with pytest.raises(LookupError, match=node.id):
        node_cost.own_many_timed([node.id], db=db, dollars={})
    with pytest.raises(LookupError, match=node.commitment_id):
        commitment_cost.project_many([node.commitment_id], db=db, ledger=usage_cost.Ledger({}, {}))
    priced = usage_cost.usage_dollars(db, {"plan_node_id": [node.id, "no-rows"]})["plan_node_id"]
    assert priced["no-rows"] == {"known": None, "unknown_rows": 0}
    assert priced[node.id]["known"] > 0
