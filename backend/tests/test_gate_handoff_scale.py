"""The acceptance-gate handoff costs the gate's own runs, never the settled history around it.

It runs inside the person's answer (``holds.clear_write``) and in ``build_run``, so it is measured like
``test_plan_write_scale``: SQLite VM steps, flat between a database with no settled history and one with ``N``
(``CORSIA_PLAN_SCALE_SIZES``). The query-plan test shows no read scans ``plan_nodes`` or ``plan_node_events``.
"""

from __future__ import annotations

import pytest

from infra.database import write_transaction
from plans import gate_handoff, gate_handoff_record, holds, node_creation, service, workflow_fields
from tests.plan_scale_fixture import build
from tests.test_plan_open_tree_scale import SAMPLE_EVERY, SIZES, STEP_TOLERANCE, measure

GATE_STEPS = [
    {"id": "work", "check": "Done", "difficulty": "routine"},
    {"id": "gate", "gate": "acceptance", "check": "Approved", "difficulty": "routine"},
]
PRIOR_RUNS = 3


def _start_run(scale, ref="gated", version="v1") -> tuple:
    db = scale.db
    phase = node_creation.create_node(
        db, scale.plan_id, kind="phase", title=f"Run {ref}", acceptance_criteria="done", parent_id=None,
        order_key=None, description=None, workflow_ref=ref, workflow_version=version, workflow_step=None,
        difficulty="routine", size="unknown", checkpoint_exception=False, assignee=None, spawn_kind=None,
        watchdog_budget_seconds=None, follow_up_of=None, actor_id=None, provenance="t", node_id=service._id(),
    )
    return phase, workflow_fields.build_run(db, phase, GATE_STEPS, causing_executor=None)


def _hold(scale, node: str) -> str:
    db = scale.db
    holds.place_hold(
        node, kind="blocked", reason_key="needs_review", reason="review", owner_kind="user", owner_id=None,
        expected_revision=service._node_row(db, node)["revision"], actor_id=None, provenance="t", db=db,
    )
    return holds.active_holds(node, db=db)[-1]["id"]


def _paths(scale):
    db = scale.db
    for _ in range(PRIOR_RUNS):
        _phase, steps = _start_run(scale)
        holds.clear_write(
            steps[1]["id"], _hold(scale, steps[1]["id"]), expected_kind="blocked", actor_id=None,
            provenance="user.ask_cleared", detail={"verdict": "approved"}, db=db,
        )
    gates = [_start_run(scale)[1][1]["id"] for _ in range(2)]
    plain = [_start_run(scale)[1][0]["id"] for _ in range(2)]
    gate_holds = iter([_hold(scale, node) for node in gates])
    plain_holds = iter([_hold(scale, node) for node in plain])
    gate_nodes, plain_nodes = iter(gates), iter(plain)
    pending, _steps = _start_run(scale)

    def clear(nodes, hold_ids):
        return lambda: holds.clear_write(
            next(nodes), next(hold_ids), expected_kind="blocked", actor_id=None,
            provenance="user.ask_cleared", detail={"verdict": "approved"}, db=db,
        )

    def gate_clear():
        node = next(gate_nodes)
        holds.clear_write(
            node, next(gate_holds), expected_kind="blocked", actor_id=None,
            provenance="user.ask_cleared", detail={"verdict": "approved"}, db=db,
        )

    return {
        "clear_write on a gate step": gate_clear,
        "clear_write on a non-gate step": clear(plain_nodes, plain_holds),
        "build_run for a gate step": lambda: workflow_fields.build_run(db, pending, GATE_STEPS, causing_executor=None),
    }


@pytest.fixture(scope="module")
def measured():
    found: dict[str, dict[int, int]] = {}
    for size in SIZES:
        scale = build(size)
        for name, call in _paths(scale).items():
            call()  # primes statement caches
            steps, _result = measure(scale.db, call)
            found.setdefault(name, {})[size] = steps
        scale.db.close()
    return found


@pytest.mark.parametrize("path", [
    "clear_write on a gate step", "clear_write on a non-gate step", "build_run for a gate step",
])
def test_gate_handoff_cost_is_flat_in_settled_history(measured, path: str) -> None:
    sizes = measured[path]
    small, large = min(sizes), max(sizes)
    print(f"\n{path}: " + ", ".join(f"{size}: {steps} steps" for size, steps in sorted(sizes.items())))
    assert sizes[large] <= sizes[small] * STEP_TOLERANCE + 2 * SAMPLE_EVERY, (path, sizes)


def test_no_gate_read_scans_a_table() -> None:
    scale = build(0)
    db = scale.db
    _phase, steps = _start_run(scale)
    key = ("p", "gated", "gate")
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    gate_handoff.derive(db, key, "v1")
    db.set_trace_callback(None)
    reads = [sql for sql in seen if "plan_node_events" in sql or "plan_nodes" in sql]
    assert len(reads) >= 5
    plans = []
    for sql in reads:  # the trace carries each statement with its values filled in
        rows = db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        plans.append((sql, [row["detail"] for row in rows]))
    scans = [detail for _sql, details in plans for detail in details if detail.startswith("SCAN")]
    assert {scan for scan in scans if not scan.startswith("SCAN json_each")} <= {"SCAN below", "SCAN CONSTANT ROW"}, scans


def _approved_runs(scale, count: int, verdict: str = "approved") -> list[str]:
    nodes = []
    for _ in range(count):
        _phase, steps = _start_run(scale)
        holds.clear_write(
            steps[1]["id"], _hold(scale, steps[1]["id"]), expected_kind="blocked", actor_id=None,
            provenance="user.ask_cleared", detail={"verdict": verdict}, db=scale.db,
        )
        nodes.append(steps[1]["id"])
        service._transition(scale.db, steps[1]["id"], "active", actor_id=None, provenance="t",
                            verification_result=None, verified_by=None)
        service._transition(scale.db, steps[1]["id"], "review_requested", actor_id=None, provenance="t",
                            verification_result=None, verified_by=None)
        from plans import review_evidence

        review_evidence.record_verdict(steps[1]["id"], "done", verdict="Accepted", actor_id=None,
                                       provenance="t", db=scale.db)
        # Settle in an owned write, as every plan writer does: the gate's document read and offer
        # run once that write commits.
        scale.db.commit()
        with write_transaction(scale.db, scope="test_settle"):
            service._transition(scale.db, steps[1]["id"], "accepted", actor_id=None, provenance="t",
                                verification_result="passed", verified_by="test")
    return nodes


def _statements(db, key) -> int:
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    gate_handoff.derive(db, key, "v1")
    db.set_trace_callback(None)
    return len(seen)


def test_reading_a_gate_is_a_fixed_number_of_statements_whatever_its_age() -> None:
    """A scope change stays excluded with the same number of statements at 14 and 30 runs."""
    from core.sent_back_corrections import record_rejection_correction

    scale = build(0)
    key = (gate_handoff_record.project_of(scale.db, scale.plan_id), "gated", "gate")
    (scope_node,) = _approved_runs(scale, 1, "approved_with_changes")
    event = service._event(scale.db, scope_node, "review_rejected", actor_id="owner", provenance="test")
    assert record_rejection_correction(scale.db, event)
    scale.db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,attributes)"
        " VALUES ('triage-scale','triage-scale','learning_loop.correction_triage','corsia','instrumented',"
        " '2026-11-01','2026-11-01',?,?)",
        (f"plan-review-rejected:{event}", '{"tier":"instruction"}'),
    )
    _approved_runs(scale, 13)
    assert len(gate_handoff.derive(scale.db, key, "v1").excluded) == 1
    young = _statements(scale.db, key)
    _approved_runs(scale, 16)
    assert gate_handoff.derive(scale.db, key, "v1").open_offer is not None
    assert _statements(scale.db, key) == young
