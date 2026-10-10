"""A model call's usage is attributed to the task current when the call is made.

A Turn may span several tasks. The Turn's own binding is written once, at
start, and stays its snapshot; the ledger copied that snapshot onto every
call's row, so a Turn that finished task A and moved to task B billed B's
calls to A. Each call now carries the executor's selection at the moment it
is recorded -- the same rule a starting Turn uses -- and falls back to the
Turn's snapshot only when the executor holds no valid selection. Slight
over-attribution to A when the agent does later work before settling A is
accepted; the plan total stays right and accounting never blocks execution.
"""

from __future__ import annotations

from infra.database import get_db
from plans import admission, service
from plans.usage_calls import record_call_usage
from tests.external_harness_usage_helpers import persist_turn

MEASURED = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


def _tasks(db, *titles: str):
    plan = service.ensure_plan("thread", "worker", db=db)
    nodes = []
    for title in titles:
        node = service.add_node(
            plan.id, kind="task", title=title, acceptance_criteria="Done", db=db,
        )
        service.transition_node(node.id, "active", db=db)
        nodes.append(node)
    db.commit()
    return plan, nodes


def _select(db, node_id: str) -> None:
    admission.select(node_id, actor_id="worker", provenance="test", db=db, touch=False)
    db.commit()


def _rows(db, turn_id: str) -> list[tuple]:
    return [
        (row["plan_node_id"], row["plan_usage_role"])
        for row in db.execute(
            "SELECT plan_node_id,plan_usage_role FROM llm_usage WHERE turn_id=? "
            "ORDER BY created_at,rowid",
            (turn_id,),
        ).fetchall()
    ]


def _call(key: str) -> None:
    """One native call: the task is captured before the provider is asked."""
    from plans.usage_calls import binding_at_call_start

    record_call_usage(
        "worker", "test-model", MEASURED, source="conversation",
        turn_id="turn-1", usage_key=key, binding=binding_at_call_start("worker"),
    )


def test_a_call_is_billed_to_the_task_current_when_it_began(isolated_db):
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        _plan, (first, second) = _tasks(db, "Task A", "Task B")
        _select(db, first.id)
        db.execute(
            "UPDATE active_turns SET plan_id=?,plan_node_id=?,plan_attempt_id=?,"
            "plan_commitment_id=?,plan_usage_role='execution' WHERE id='turn-1'",
            (_plan.id, first.id, "attempt-a", first.commitment_id),
        )
        db.commit()

        _call("provider-call:1")
        admission.release(first.id, "accepted", db=db)
        db.commit()
        _select(db, second.id)
        _call("provider-call:2")

        assert _rows(db, "turn-1") == [
            (first.id, "execution"), (second.id, "execution"),
        ]
    finally:
        db.close()


def test_a_call_with_no_live_selection_keeps_the_turns_snapshot(isolated_db):
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        _plan, (first,) = _tasks(db, "Task A")
        db.execute(
            "UPDATE active_turns SET plan_id=?,plan_node_id=?,plan_attempt_id=?,"
            "plan_commitment_id=?,plan_usage_role='execution' WHERE id='turn-1'",
            (_plan.id, first.id, "attempt-a", first.commitment_id),
        )
        db.commit()

        _call("provider-call:1")

        assert _rows(db, "turn-1") == [(first.id, "execution")]
    finally:
        db.close()


def test_an_unbound_turn_binds_its_calls_to_the_selection_it_gains(isolated_db):
    """A call made before any selection falls back to the Turn's snapshot, and
    the selection the Turn gains is that snapshot, so it carries it too."""
    persist_turn("worker", "turn-1")
    db = get_db()
    try:
        _call("provider-call:1")
        _plan, (first,) = _tasks(db, "Task A")
        _select(db, first.id)
        _call("provider-call:2")

        assert _rows(db, "turn-1") == [(first.id, "execution"), (first.id, "execution")]
    finally:
        db.close()


def test_an_unreadable_attribution_is_recorded_as_unknown_not_as_the_turns(monkeypatch):
    """Reading the binding failing is attribution loss, stated as such -- never a stopped call."""
    from plans import usage_calls

    def broken(_thread_id, *, db=None):
        raise RuntimeError("accounting read failed")

    monkeypatch.setattr(usage_calls, "call_binding", broken)

    class _Db:
        def close(self):
            return None

    monkeypatch.setattr("infra.database.get_db", lambda: _Db())
    captured = usage_calls.binding_at_call_start("worker")

    assert captured == usage_calls.UNATTRIBUTED
    assert captured is not usage_calls.UNATTRIBUTED
    assert all(value is None for value in captured.values())


def test_the_entry_records_exactly_the_binding_the_caller_captured(monkeypatch):
    """Captured at call start, passed through untouched; None is the Turn's snapshot."""
    from plans import usage_calls

    recorded: list[dict] = []
    monkeypatch.setattr(
        usage_calls, "call_binding",
        lambda _thread_id, *, db=None: {"plan_id": "p", "node_id": "task-b"},
    )
    monkeypatch.setattr(
        usage_calls, "record_llm_usage",
        lambda *args, **kwargs: recorded.append(kwargs),
    )

    started = {"plan_id": "p", "node_id": "task-a"}
    usage_calls.record_call_usage(
        "worker", "test/model", {"prompt_tokens": 1}, source="conversation",
        turn_id="turn-1", usage_key="provider-call:1", binding=started,
    )
    usage_calls.record_call_usage(
        "worker", "test/model", {"prompt_tokens": 1}, source="external_harness:codex",
        turn_id="turn-1", usage_key="terminal", binding=None,
    )

    assert recorded[0]["binding"] == started
    assert recorded[1]["binding"] is None


def test_a_call_start_capture_reads_the_executors_current_selection(monkeypatch):
    from plans import usage_calls

    monkeypatch.setattr(
        usage_calls, "call_binding",
        lambda _thread_id, *, db=None: {"plan_id": "p", "node_id": "task-a"},
    )

    class _Db:
        def close(self):
            return None

    monkeypatch.setattr("infra.database.get_db", lambda: _Db())
    assert usage_calls.binding_at_call_start("worker") == {
        "plan_id": "p", "node_id": "task-a",
    }
